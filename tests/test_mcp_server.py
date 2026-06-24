"""Tests for the MCP server builder and run() transport dispatch."""
from unittest.mock import patch, MagicMock

import pytest


# ── AuthKeyMiddleware ───────────────────────────────────────────────────
# The middleware is the sole network security control for HTTP transports,
# so it gets its own direct unit tests (not just "the middleware list is
# non-empty" coverage from the run() tests). Drives the ASGI __call__ with a
# fake downstream app + a send-capture, exercising the pass-through / 401
# branches and the scheme/prefix handling.

def _run_middleware(scope, auth_key):
    """Drive AuthKeyMiddleware with one scope; return (downstream_called, sent)."""
    import asyncio
    from agent_windows.mcp.server import AuthKeyMiddleware

    downstream = {"called": False}

    async def app(scope, receive, send):
        downstream["called"] = True

    sent = []

    async def send(msg):
        sent.append(msg)

    mw = AuthKeyMiddleware(app, auth_key)
    asyncio.run(mw(scope, receive=lambda: None, send=send))
    return downstream["called"], sent


def _http_scope(headers=None):
    """Build a minimal ASGI http scope with the given headers."""
    raw = []
    for k, v in (headers or {}).items():
        raw.append([k.encode("latin-1"), v.encode("latin-1")])
    return {"type": "http", "headers": raw}


def test_auth_middleware_passes_valid_bearer_token():
    called, sent = _run_middleware(
        _http_scope({"authorization": "Bearer s3cret"}), "s3cret")
    assert called is True          # downstream app invoked
    assert sent == []              # no 401 response emitted


def test_auth_middleware_rejects_missing_header():
    called, sent = _run_middleware(_http_scope(), "s3cret")
    assert called is False
    assert sent[0]["status"] == 401
    assert sent[0]["type"] == "http.response.start"
    # www-authenticate challenge present
    assert [b"www-authenticate", b"Bearer"] in sent[0]["headers"]
    assert sent[1]["body"] == b"Unauthorized"


def test_auth_middleware_rejects_wrong_token():
    called, sent = _run_middleware(
        _http_scope({"authorization": "Bearer wrong"}), "s3cret")
    assert called is False
    assert sent[0]["status"] == 401


def test_auth_middleware_rejects_non_bearer_scheme():
    # "Basic ..." or a bare token without the Bearer prefix must not pass.
    called, _ = _run_middleware(
        _http_scope({"authorization": "s3cret"}), "s3cret")
    assert called is False
    called2, _ = _run_middleware(
        _http_scope({"authorization": "Basic s3cret"}), "s3cret")
    assert called2 is False


def test_auth_middleware_is_case_insensitive_on_header_name():
    # ASGI servers lowercase header names, but the middleware must not depend on
    # case from the wire — "Authorization" and "AUTHORIZATION" are the same.
    called, _ = _run_middleware(
        _http_scope({"Authorization": "Bearer s3cret"}), "s3cret")
    assert called is True


def test_auth_middleware_passes_lifespan_unauthenticated():
    # Lifespan startup carries no request headers — must always pass through.
    called, sent = _run_middleware({"type": "lifespan", "headers": []}, "s3cret")
    assert called is True
    assert sent == []


def test_auth_middleware_passes_non_http_scope():
    # ws / other scopes are not the middleware's concern — pass through.
    called, _ = _run_middleware({"type": "websocket", "headers": []}, "s3cret")
    assert called is True


def test_build_server_returns_fastmcp_with_tools_registered():
    from agent_windows.mcp.server import build_server, reset_server
    import fastmcp
    reset_server()
    srv = build_server()
    assert isinstance(srv, fastmcp.FastMCP)
    # building is idempotent (memoized)
    assert build_server() is srv
    reset_server()


def test_run_stdio_calls_run_with_stdio_transport():
    from agent_windows.mcp import server
    fake = MagicMock()
    with patch.object(server, "build_server", return_value=fake):
        server.run(transport="stdio")
    fake.run.assert_called_once()
    kwargs = fake.run.call_args.kwargs
    assert kwargs["transport"] == "stdio"
    assert kwargs["show_banner"] is False


def test_run_http_forwards_host_port_and_auth_middleware():
    from agent_windows.mcp import server
    fake = MagicMock()
    with patch.object(server, "build_server", return_value=fake):
        server.run(transport="streamable-http", host="127.0.0.1", port=9000,
                   auth_key="s3cret")
    kwargs = fake.run.call_args.kwargs
    assert kwargs["transport"] == "streamable-http"
    assert kwargs["host"] == "127.0.0.1"
    assert kwargs["port"] == 9000
    # auth_key set → an ASGI middleware list was passed
    assert "middleware" in kwargs and kwargs["middleware"]


def test_run_http_without_auth_key_has_no_middleware():
    from agent_windows.mcp import server
    fake = MagicMock()
    with patch.object(server, "build_server", return_value=fake):
        server.run(transport="streamable-http", host="127.0.0.1", port=9000,
                   auth_key=None)
    kwargs = fake.run.call_args.kwargs
    # no auth_key → no middleware (bare localhost bind, no auth)
    assert not kwargs.get("middleware")


def test_run_rejects_unknown_transport():
    from agent_windows.mcp import server
    with pytest.raises(ValueError):
        server.run(transport="carrier-pigeon")


def test_mcp_serve_delegates_explicit_flags_overriding_config():
    """`agent-windows mcp serve` forwards explicit flags to run(); config fills the rest."""
    from typer.testing import CliRunner
    from agent_windows.cli.commands import mcp_cmd

    runner = CliRunner()
    # NOTE: `serve` imports `run` lazily INSIDE the function (so importing the CLI
    # module doesn't hard-require the optional fastmcp dep — see mcp_cmd.py), so
    # `mcp_cmd.run` is not a module attribute and patch.object would fail at
    # setup. Patch the name `serve` actually re-binds at call time: it imports
    # from `agent_windows.mcp` (the re-export in mcp/__init__.py), so patch THAT
    # attribute. load_config IS a real top-level import in mcp_cmd, so it patches
    # on the command module as written.
    with patch("agent_windows.mcp.run") as mock_run, \
         patch.object(mcp_cmd, "load_config",
                      return_value={"mcp": {"auth_key": "cfgkey", "transport": "sse"}}):
        result = runner.invoke(mcp_cmd.app,
                               ["serve", "--transport", "streamable-http",
                                "--host", "0.0.0.0", "--port", "9000", "--debug"])
    assert result.exit_code == 0, result.output
    mock_run.assert_called_once_with(transport="streamable-http", host="0.0.0.0",
                                     port=9000, debug=True,
                                     auth_key="cfgkey", ssl_certfile=None, ssl_keyfile=None)


def test_mcp_serve_defaults_come_from_config():
    """No flags → use config [mcp] defaults (transport/host/port/auth_key)."""
    from typer.testing import CliRunner
    from agent_windows.cli.commands import mcp_cmd

    runner = CliRunner()
    with patch("agent_windows.mcp.run") as mock_run, \
         patch.object(mcp_cmd, "load_config",
                      return_value={"mcp": {"transport": "streamable-http",
                                            "host": "1.2.3.4", "port": 7000,
                                            "auth_key": "k", "ssl_certfile": "c.pem",
                                            "ssl_keyfile": "k.pem"}}):
        runner.invoke(mcp_cmd.app, ["serve"])
    mock_run.assert_called_once_with(transport="streamable-http", host="1.2.3.4",
                                     port=7000, debug=False, auth_key="k",
                                     ssl_certfile="c.pem", ssl_keyfile="k.pem")


def test_mcp_serve_stdio_strips_auth_and_ssl():
    """stdio transport never forwards auth_key/ssl (they're HTTP-only)."""
    from typer.testing import CliRunner
    from agent_windows.cli.commands import mcp_cmd

    runner = CliRunner()
    with patch("agent_windows.mcp.run") as mock_run, \
         patch.object(mcp_cmd, "load_config",
                      return_value={"mcp": {"auth_key": "k", "ssl_certfile": "c.pem",
                                            "ssl_keyfile": "k.pem"}}):
        runner.invoke(mcp_cmd.app, ["serve", "--transport", "stdio"])
    mock_run.assert_called_once_with(transport="stdio", host="127.0.0.1",
                                     port=8000, debug=False,
                                     auth_key=None, ssl_certfile=None, ssl_keyfile=None)


def test_mcp_serve_rejects_out_of_range_port():
    """A --port outside 1..65535 fails with a clear error instead of reaching uvicorn."""
    from typer.testing import CliRunner
    from agent_windows.cli.commands import mcp_cmd

    runner = CliRunner()
    for bad in ("-1", "0", "70000"):
        with patch("agent_windows.mcp.run") as mock_run, \
             patch.object(mcp_cmd, "load_config", return_value={}):
            result = runner.invoke(mcp_cmd.app,
                                   ["serve", "--transport", "streamable-http", "--port", bad])
        assert result.exit_code != 0, f"--port {bad} should be rejected"
        assert "port" in result.output.lower()
        mock_run.assert_not_called()  # never reach the server



# ── is_loopback_host + non-loopback bind guard ──────────────────────────

def test_is_loopback_host_recognizes_loopback_addresses():
    from agent_windows.mcp.server import is_loopback_host
    assert is_loopback_host("localhost") is True
    assert is_loopback_host("LOCALHOST") is True          # case-insensitive
    assert is_loopback_host("127.0.0.1") is True
    assert is_loopback_host("::1") is True


def test_is_loopback_host_rejects_non_loopback():
    from agent_windows.mcp.server import is_loopback_host
    assert is_loopback_host("0.0.0.0") is False
    assert is_loopback_host("192.168.1.5") is False
    assert is_loopback_host("myhost") is False            # non-IP string → False


def test_mcp_serve_refuses_bare_non_loopback_http_bind():
    """Non-loopback host, no auth_key, no flag → serve aborts before run()."""
    from typer.testing import CliRunner
    from agent_windows.cli.commands import mcp_cmd

    runner = CliRunner()
    with patch("agent_windows.mcp.run") as mock_run, \
         patch.object(mcp_cmd, "load_config", return_value={}):
        result = runner.invoke(mcp_cmd.app,
                               ["serve", "--transport", "streamable-http",
                                "--host", "0.0.0.0", "--port", "9000"])
    assert result.exit_code != 0
    out = result.output.lower()
    assert "refus" in out or "insecure" in out
    mock_run.assert_not_called()


def test_mcp_serve_non_loopback_with_auth_key_proceeds():
    """auth_key set → non-loopback bind is allowed."""
    from typer.testing import CliRunner
    from agent_windows.cli.commands import mcp_cmd

    runner = CliRunner()
    with patch("agent_windows.mcp.run") as mock_run, \
         patch.object(mcp_cmd, "load_config",
                      return_value={"mcp": {"auth_key": "k"}}):
        result = runner.invoke(mcp_cmd.app,
                               ["serve", "--transport", "streamable-http",
                                "--host", "0.0.0.0", "--port", "9000"])
    assert result.exit_code == 0, result.output
    mock_run.assert_called_once()


def test_mcp_serve_non_loopback_with_allow_insecure_remote_proceeds_with_warning():
    """--allow-insecure-remote opts in and prints a loud WARNING."""
    from typer.testing import CliRunner
    from agent_windows.cli.commands import mcp_cmd

    runner = CliRunner()
    with patch("agent_windows.mcp.run") as mock_run, \
         patch.object(mcp_cmd, "load_config", return_value={}):
        result = runner.invoke(mcp_cmd.app,
                               ["serve", "--transport", "streamable-http",
                                "--host", "0.0.0.0", "--port", "9000",
                                "--allow-insecure-remote"])
    assert result.exit_code == 0, result.output
    mock_run.assert_called_once()
    assert "warning" in result.output.lower()


def test_mcp_serve_loopback_host_needs_no_auth():
    """127.0.0.1 with no auth_key is fine — loopback only."""
    from typer.testing import CliRunner
    from agent_windows.cli.commands import mcp_cmd

    runner = CliRunner()
    with patch("agent_windows.mcp.run") as mock_run, \
         patch.object(mcp_cmd, "load_config", return_value={}):
        result = runner.invoke(mcp_cmd.app,
                               ["serve", "--transport", "streamable-http",
                                "--host", "127.0.0.1", "--port", "9000"])
    assert result.exit_code == 0, result.output
    mock_run.assert_called_once()


def test_mcp_serve_allow_insecure_remote_config_fallback():
    """[mcp] allow_insecure_remote=true in config satisfies the guard (no CLI flag)."""
    from typer.testing import CliRunner
    from agent_windows.cli.commands import mcp_cmd

    runner = CliRunner()
    with patch("agent_windows.mcp.run") as mock_run, \
         patch.object(mcp_cmd, "load_config",
                      return_value={"mcp": {"allow_insecure_remote": True}}):
        result = runner.invoke(mcp_cmd.app,
                               ["serve", "--transport", "streamable-http",
                                "--host", "0.0.0.0", "--port", "9000"])
    assert result.exit_code == 0, result.output
    mock_run.assert_called_once()
    assert "warning" in result.output.lower()


# ── `mcp auth` ──────────────────────────────────────────────────────────

def test_mcp_auth_generates_key_and_writes_config(tmp_path, monkeypatch):
    """`mcp auth` writes a fresh auth_key into the [mcp] section."""
    from typer.testing import CliRunner
    from agent_windows.utils import config as cfgmod
    from agent_windows.cli.commands import mcp_cmd

    # isolate config to a temp file
    monkeypatch.setattr(cfgmod, "get_config_path", lambda: tmp_path / "config.toml")
    cfgmod._config_cache = None

    runner = CliRunner()
    # `auth` imports save_config (and load_config) lazily INSIDE the function,
    # so patch at the source — the lazy `from agent_windows.utils.config import
    # save_config` re-reads that module attribute at call time.
    with patch("agent_windows.utils.config.save_config") as save, \
         patch("agent_windows.utils.config.load_config",
               return_value={"mcp": {"auth_key": ""}}):
        result = runner.invoke(mcp_cmd.app, ["auth", "--transport", "streamable-http",
                                             "--host", "1.2.3.4", "--port", "9000"])
    assert result.exit_code == 0, result.output
    save.assert_called_once()
    written = save.call_args.args[0]
    assert written["mcp"]["transport"] == "streamable-http"
    assert written["mcp"]["host"] == "1.2.3.4"
    assert written["mcp"]["port"] == 9000
    # auth_key is a non-empty generated token
    assert len(written["mcp"]["auth_key"]) >= 32


def test_mcp_auth_refuses_overwrite_without_force(tmp_path, monkeypatch):
    from typer.testing import CliRunner
    from agent_windows.utils import config as cfgmod
    from agent_windows.cli.commands import mcp_cmd

    monkeypatch.setattr(cfgmod, "get_config_path", lambda: tmp_path / "config.toml")
    cfgmod._config_cache = None
    # pre-seed an existing key via the REAL save_config (writes to the temp path)
    cfg = dict(cfgmod.DEFAULT_CONFIG)
    cfg["mcp"] = dict(cfg["mcp"], auth_key="existing")
    cfgmod.save_config(cfg)
    cfgmod._config_cache = None

    with patch("agent_windows.utils.config.save_config") as save:
        result = CliRunner().invoke(mcp_cmd.app, ["auth"])
    assert result.exit_code == 0
    # existing key → save NOT called, message printed instead
    save.assert_not_called()
    assert "already set" in result.output.lower() or "use --force" in result.output.lower()


def test_mcp_auth_prints_stdio_client_snippet():
    from typer.testing import CliRunner
    from agent_windows.cli.commands import mcp_cmd
    from agent_windows.utils import config as cfgmod
    import copy

    runner = CliRunner()
    # Pass a deep copy: auth() mutates the config it's handed (sets
    # mcp.host/port/transport/auth_key), so handing it the live DEFAULT_CONFIG
    # would corrupt it for any later test that reads the defaults.
    with patch("agent_windows.utils.config.save_config"), \
         patch("agent_windows.utils.config.load_config",
               return_value=copy.deepcopy(cfgmod.DEFAULT_CONFIG)):
        result = runner.invoke(mcp_cmd.app, ["auth", "--transport", "stdio"])
    # the printed client config must contain an mcpServers block
    assert "mcpServers" in result.output
    assert "mcp" in result.output and "serve" in result.output


# ── `mcp install` / `mcp uninstall` ─────────────────────────────────────

def test_mcp_install_registers_scheduled_task(monkeypatch, tmp_path):
    """install calls Register-ScheduledTask with an ONLOGON trigger."""
    from typer.testing import CliRunner
    from agent_windows.cli.commands import mcp_cmd

    runs = []
    def fake_run(cmd, **kw):
        runs.append(cmd)
        class R:
            returncode = 0
            stdout = ""
            stderr = ""
        return R()

    monkeypatch.setattr(mcp_cmd.subprocess, "run", fake_run)
    script = tmp_path / "start-mcp.cmd"
    monkeypatch.setattr(mcp_cmd, "_start_script_path", lambda: script)
    monkeypatch.setattr(mcp_cmd, "_build_start_script", lambda args: "@echo off\n")

    result = CliRunner().invoke(mcp_cmd.app,
                                ["install", "--transport", "streamable-http",
                                 "--host", "127.0.0.1", "--port", "8000", "--force"])
    assert result.exit_code == 0, result.output
    # the Register-ScheduledTask PowerShell call happened
    assert any("Register-ScheduledTask" in " ".join(c) for c in runs)
    assert any("agent-windows-mcp" in " ".join(c) for c in runs)


def test_mcp_uninstall_deletes_task_and_script(monkeypatch, tmp_path):
    from typer.testing import CliRunner
    from agent_windows.cli.commands import mcp_cmd

    calls = []
    def fake_run(cmd, **kw):
        calls.append(cmd)
        class R:
            returncode = 0
            stdout = ""
            stderr = ""
        return R()

    monkeypatch.setattr(mcp_cmd.subprocess, "run", fake_run)
    script = tmp_path / "start-mcp.cmd"
    script.write_text("x")  # pre-create so uninstall's exists() check is true
    monkeypatch.setattr(mcp_cmd, "_start_script_path", lambda: script)

    result = CliRunner().invoke(mcp_cmd.app, ["uninstall"])
    assert result.exit_code == 0, result.output
    # schtasks /Delete was issued
    assert any("/Delete" in " ".join(c) and "agent-windows-mcp" in " ".join(c) for c in calls)
    # the wrapper script was removed
    assert not script.exists()
