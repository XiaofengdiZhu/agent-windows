"""Tests for the MCP server builder and run() transport dispatch."""
from unittest.mock import patch, MagicMock

import pytest


# ── AuthKeyMiddleware ───────────────────────────────────────────────────
# The middleware is the sole network security control for HTTP transports,
# so it gets its own direct unit tests (not just "the middleware list is
# non-empty" coverage from the run() tests). Drives the ASGI __call__ with a
# fake downstream app + a send-capture, exercising the pass-through / 401
# branches and the scheme/prefix handling.

def _drive(middleware_factory, scope):
    """Drive a raw-ASGI middleware with one scope; return (downstream_called, sent).

    Shared harness for the security middlewares. ``middleware_factory`` receives
    the fake downstream app and returns the middleware instance. A tracking app
    records whether the middleware called through; a send-capture collects the
    response messages it emits (e.g. the 401/403 start+body).
    """
    import asyncio

    downstream = {"called": False}

    async def tracking_app(scope, receive, send):
        downstream["called"] = True

    mw = middleware_factory(tracking_app)
    sent = []

    async def send(msg):
        sent.append(msg)

    asyncio.run(mw(scope, receive=lambda: None, send=send))
    return downstream["called"], sent


def _run_middleware(scope, auth_key):
    """Drive AuthKeyMiddleware with one scope; return (downstream_called, sent)."""
    from agent_windows.mcp.server import AuthKeyMiddleware
    return _drive(lambda app: AuthKeyMiddleware(app, auth_key=auth_key), scope)


def _http_scope(headers=None, client=None, path="/mcp"):
    """Build a minimal ASGI http scope with the given headers/client/path."""
    raw = []
    for k, v in (headers or {}).items():
        raw.append([k.encode("latin-1"), v.encode("latin-1")])
    scope = {"type": "http", "headers": raw, "path": path}
    if client is not None:
        scope["client"] = client
    return scope


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


def test_run_http_without_auth_key_only_has_options_middleware():
    from agent_windows.mcp import server
    from agent_windows.mcp.server import OptionsMiddleware
    fake = MagicMock()
    with patch.object(server, "build_server", return_value=fake):
        server.run(transport="streamable-http", host="127.0.0.1", port=9000,
                   auth_key=None)
    kwargs = fake.run.call_args.kwargs
    # No auth_key and no CORS → OptionsMiddleware is the ONLY middleware (with
    # an empty allowlist it emits no CORS headers, so SOP blocks cross-origin).
    # AuthKeyMiddleware / IPAllowlistMiddleware / CORSMiddleware are absent.
    kinds = [mw.cls for mw in kwargs["middleware"]]
    assert kinds == [OptionsMiddleware]


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
                                     auth_key="cfgkey", ssl_certfile=None, ssl_keyfile=None,
                                     ip_allowlist=None, cors_origins=None,
                                     tools=None, exclude_tools=None, stateless_http=False,
                                     oauth_client_id=None, oauth_client_secret=None,
                                     allowed_hosts=None)


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
                                     ssl_certfile="c.pem", ssl_keyfile="k.pem",
                                     ip_allowlist=None, cors_origins=None,
                                     tools=None, exclude_tools=None, stateless_http=False,
                                     oauth_client_id=None, oauth_client_secret=None,
                                     allowed_hosts=["1.2.3.4"])


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
                                     auth_key=None, ssl_certfile=None, ssl_keyfile=None,
                                     ip_allowlist=None, cors_origins=None,
                                     tools=None, exclude_tools=None, stateless_http=False,
                                     oauth_client_id=None, oauth_client_secret=None,
                                     allowed_hosts=None)


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


def test_mcp_serve_allow_insecure_remote_env_fallback(monkeypatch):
    """AGENT_WINDOWS_ALLOW_INSECURE_REMOTE=true satisfies the guard (no flag/config)."""
    from typer.testing import CliRunner
    from agent_windows.cli.commands import mcp_cmd

    monkeypatch.setenv("AGENT_WINDOWS_ALLOW_INSECURE_REMOTE", "true")
    runner = CliRunner()
    with patch("agent_windows.mcp.run") as mock_run, \
         patch.object(mcp_cmd, "load_config", return_value={}):
        result = runner.invoke(mcp_cmd.app,
                               ["serve", "--transport", "streamable-http",
                                "--host", "0.0.0.0", "--port", "9000"])
    assert result.exit_code == 0, result.output
    mock_run.assert_called_once()
    assert "warning" in result.output.lower()


def test_mcp_serve_allow_insecure_remote_no_warning_on_loopback():
    """The insecure-bind warning is misleading for a loopback address (only the
    local machine can reach it), so it must not fire there even with the flag."""
    from typer.testing import CliRunner
    from agent_windows.cli.commands import mcp_cmd

    runner = CliRunner()
    with patch("agent_windows.mcp.run") as mock_run, \
         patch.object(mcp_cmd, "load_config", return_value={}):
        result = runner.invoke(mcp_cmd.app,
                               ["serve", "--transport", "streamable-http",
                                "--host", "127.0.0.1", "--port", "9000",
                                "--allow-insecure-remote"])
    assert result.exit_code == 0, result.output
    mock_run.assert_called_once()
    assert "warning" not in result.output.lower()


# ── parse_ip_allowlist + IPAllowlistMiddleware ─────────────────────────

def test_parse_ip_allowlist_accepts_ips_cidrs_ipv4_ipv6():
    import ipaddress
    from agent_windows.mcp.server import parse_ip_allowlist

    nets = parse_ip_allowlist(["192.168.1.5", "10.0.0.0/8", "2001:db8::/32"])
    assert nets[0] == ipaddress.ip_network("192.168.1.5/32")
    assert nets[1] == ipaddress.ip_network("10.0.0.0/8")
    assert nets[2] == ipaddress.ip_network("2001:db8::/32")


def test_parse_ip_allowlist_ignores_blank_strips_whitespace():
    import ipaddress
    from agent_windows.mcp.server import parse_ip_allowlist

    assert parse_ip_allowlist(["  10.0.0.0/8  ", "", "  "]) == [
        ipaddress.ip_network("10.0.0.0/8")]


def test_parse_ip_allowlist_raises_naming_bad_entries():
    from agent_windows.mcp.server import parse_ip_allowlist

    with pytest.raises(ValueError) as exc:
        parse_ip_allowlist(["10.0.0.0/8", "not-an-ip"])
    assert "not-an-ip" in str(exc.value)


def _run_ip_middleware(scope, allowlist):
    """Drive IPAllowlistMiddleware; return (downstream_called, sent)."""
    from agent_windows.mcp.server import IPAllowlistMiddleware, parse_ip_allowlist
    nets = parse_ip_allowlist(allowlist)
    return _drive(lambda app: IPAllowlistMiddleware(app, allowlist=nets), scope)


def test_ip_allowlist_middleware_passes_allowed_client():
    called, sent = _run_ip_middleware(
        _http_scope(client=("192.168.1.5", 12345)), ["192.168.1.0/24"])
    assert called is True
    assert sent == []


def test_ip_allowlist_middleware_rejects_disallowed_client():
    called, sent = _run_ip_middleware(
        _http_scope(client=("8.8.8.8", 12345)), ["192.168.1.0/24"])
    assert called is False
    assert sent[0]["status"] == 403


def test_ip_allowlist_middleware_exempts_health():
    # /health must always pass regardless of source (liveness probes).
    called, _ = _run_ip_middleware(
        _http_scope(client=("8.8.8.8", 12345), path="/health"), ["192.168.1.0/24"])
    assert called is True


def test_ip_allowlist_middleware_rejects_missing_client():
    called, sent = _run_ip_middleware(
        _http_scope(), ["192.168.1.0/24"])
    assert called is False
    assert sent[0]["status"] == 403


def test_ip_allowlist_middleware_passes_lifespan():
    # Lifespan startup has no client — must pass through (mirrors AuthKeyMiddleware).
    called, sent = _run_ip_middleware(
        {"type": "lifespan", "headers": []}, ["192.168.1.0/24"])
    assert called is True
    assert sent == []


# ── _PUBLIC_PATHS + IPAllowlist reuse ───────────────────────────────────


def test_public_paths_includes_oauth_endpoints():
    from agent_windows.mcp.server import _PUBLIC_PATHS
    for p in ("/health", "/.well-known/oauth-authorization-server",
              "/oauth/register", "/oauth/authorize", "/oauth/token"):
        assert p in _PUBLIC_PATHS


def test_ip_allowlist_middleware_exempts_oauth_endpoints():
    # OAuth endpoints must be reachable for a remote client even when an IP
    # allowlist is set, otherwise OAuth + IP-allowlist cannot coexist.
    for p in ("/oauth/authorize", "/oauth/token", "/oauth/register",
              "/.well-known/oauth-authorization-server"):
        called, _ = _run_ip_middleware(
            _http_scope(client=("8.8.8.8", 12345), path=p), ["192.168.1.0/24"])
        assert called is True, f"{p} should be exempt from IP allowlist"


# ── AuthKeyMiddleware oauth fallback + OAuthOnlyMiddleware ──────────────


def _run_auth_middleware_oauth(scope, auth_key, oauth_validator=None):
    """Drive AuthKeyMiddleware with optional oauth_validator."""
    from agent_windows.mcp.server import AuthKeyMiddleware
    return _drive(
        lambda app: AuthKeyMiddleware(app, auth_key=auth_key,
                                      oauth_validator=oauth_validator),
        scope)


def test_auth_middleware_oauth_validator_accepts_oauth_token():
    # static key mismatch but oauth validator says valid → pass through.
    validator = lambda tok: tok == "oauth-tok"  # noqa: E731
    called, sent = _run_auth_middleware_oauth(
        _http_scope({"authorization": "Bearer oauth-tok"}),
        "static-key", oauth_validator=validator)
    assert called is True and sent == []


def test_auth_middleware_oauth_validator_rejects_when_both_invalid():
    validator = lambda tok: False  # noqa: E731
    called, sent = _run_auth_middleware_oauth(
        _http_scope({"authorization": "Bearer nope"}),
        "static-key", oauth_validator=validator)
    assert called is False
    assert sent[0]["status"] == 401


def test_auth_middleware_bypasses_public_paths_without_token():
    # OAuth endpoints must be reachable before auth (no Authorization header).
    for p in ("/oauth/authorize", "/oauth/token", "/oauth/register",
              "/.well-known/oauth-authorization-server", "/health"):
        called, sent = _run_auth_middleware_oauth(_http_scope(path=p), "k")
        assert called is True, f"{p} should be a public path"
        assert sent == []


def test_auth_middleware_oauth_validator_none_still_works():
    # Backward compat: oauth_validator=None (default) → static key still enforced.
    called, sent = _run_auth_middleware_oauth(
        _http_scope({"authorization": "Bearer s3cret"}), "s3cret")
    assert called is True and sent == []


def _run_oauth_only_middleware(scope, validator):
    """Drive OAuthOnlyMiddleware."""
    from agent_windows.mcp.server import OAuthOnlyMiddleware
    return _drive(lambda app: OAuthOnlyMiddleware(app, oauth_validator=validator),
                  scope)


def test_oauth_only_middleware_accepts_valid_oauth_token():
    validator = lambda tok: tok == "good"  # noqa: E731
    called, sent = _run_oauth_only_middleware(
        _http_scope({"authorization": "Bearer good"}), validator)
    assert called is True and sent == []


def test_oauth_only_middleware_rejects_missing_or_invalid():
    validator = lambda tok: tok == "good"  # noqa: E731
    # missing
    called, sent = _run_oauth_only_middleware(_http_scope(), validator)
    assert called is False and sent[0]["status"] == 401
    # invalid
    called, sent = _run_oauth_only_middleware(
        _http_scope({"authorization": "Bearer bad"}), validator)
    assert called is False and sent[0]["status"] == 401


def test_oauth_only_middleware_bypasses_public_paths():
    validator = lambda tok: False  # noqa: E731
    for p in ("/oauth/authorize", "/oauth/token", "/health"):
        called, sent = _run_oauth_only_middleware(_http_scope(path=p), validator)
        assert called is True, f"{p} should be public"


def test_oauth_only_middleware_passes_lifespan_and_ws():
    validator = lambda tok: True  # noqa: E731
    called, sent = _run_oauth_only_middleware(
        {"type": "lifespan", "headers": []}, validator)
    assert called is True and sent == []
    called, _ = _run_oauth_only_middleware(
        {"type": "websocket", "headers": []}, validator)
    assert called is True


# ── run() OAuth wiring ──────────────────────────────────────────────────


def test_run_oauth_mounts_routes_and_oauthonly_middleware():
    from agent_windows.mcp import server
    from agent_windows.mcp.server import OAuthOnlyMiddleware
    fake = MagicMock()
    fake.custom_route = MagicMock()
    with patch.object(server, "build_server", return_value=fake):
        server.run(transport="streamable-http", host="localhost", port=8000,
                   oauth_client_id="cid", oauth_client_secret="sec")
    # 4 OAuth routes mounted via custom_route
    paths = {c.args[0] for c in fake.custom_route.call_args_list}
    assert paths == {"/.well-known/oauth-authorization-server",
                     "/oauth/register", "/oauth/authorize", "/oauth/token"}
    kwargs = fake.run.call_args.kwargs
    kinds = [mw.cls for mw in kwargs["middleware"]]
    # No auth_key → OAuthOnlyMiddleware is the inner auth layer.
    assert OAuthOnlyMiddleware in kinds


def test_run_oauth_with_auth_key_uses_authkey_with_validator():
    from agent_windows.mcp import server
    from agent_windows.mcp.server import AuthKeyMiddleware
    fake = MagicMock()
    fake.custom_route = MagicMock()
    with patch.object(server, "build_server", return_value=fake):
        server.run(transport="streamable-http", host="localhost", port=8000,
                   auth_key="k", oauth_client_id="cid", oauth_client_secret="sec")
    kwargs = fake.run.call_args.kwargs
    auth_mw = [mw for mw in kwargs["middleware"] if mw.cls is AuthKeyMiddleware]
    assert len(auth_mw) == 1
    # oauth_validator was passed (a callable)
    assert callable(auth_mw[0].kwargs.get("oauth_validator"))


def test_run_oauth_routes_not_mounted_when_not_configured():
    from agent_windows.mcp import server
    fake = MagicMock()
    with patch.object(server, "build_server", return_value=fake):
        server.run(transport="streamable-http", host="localhost", port=8000)
    # No OAuth configured → no custom_route calls, no OAuthOnlyMiddleware.
    fake.custom_route.assert_not_called()
    from agent_windows.mcp.server import OAuthOnlyMiddleware
    kwargs = fake.run.call_args.kwargs
    assert OAuthOnlyMiddleware not in [mw.cls for mw in kwargs["middleware"]]


def test_run_oauth_ignored_on_stdio():
    from agent_windows.mcp import server
    fake = MagicMock()
    with patch.object(server, "build_server", return_value=fake):
        server.run(transport="stdio", oauth_client_id="cid",
                   oauth_client_secret="sec")
    # stdio never mounts OAuth routes.
    fake.custom_route.assert_not_called()
    fake.run.assert_called_once()


# ── serve() OAuth flags / precedence / guard ────────────────────────────


def test_serve_oauth_flags_forwarded():
    """--oauth-client-id/--secret are forwarded to run()."""
    from typer.testing import CliRunner
    from agent_windows.cli.commands import mcp_cmd
    with patch("agent_windows.mcp.run") as mock_run, \
         patch.object(mcp_cmd, "load_config", return_value={}):
        result = CliRunner().invoke(mcp_cmd.app, [
            "serve", "--transport", "streamable-http", "--host", "0.0.0.0",
            "--port", "9000", "--oauth-client-id", "cid",
            "--oauth-client-secret", "sec"])
    assert result.exit_code == 0, result.output
    assert mock_run.call_args.kwargs["oauth_client_id"] == "cid"
    assert mock_run.call_args.kwargs["oauth_client_secret"] == "sec"


def test_serve_oauth_from_config():
    """[security] oauth_client_id/secret satisfy the guard + forward."""
    from typer.testing import CliRunner
    from agent_windows.cli.commands import mcp_cmd
    with patch("agent_windows.mcp.run") as mock_run, \
         patch.object(mcp_cmd, "load_config",
                      return_value={"security": {"oauth_client_id": "cid",
                                                 "oauth_client_secret": "sec"}}):
        result = CliRunner().invoke(mcp_cmd.app, [
            "serve", "--transport", "streamable-http", "--host", "0.0.0.0",
            "--port", "9000"])
    assert result.exit_code == 0, result.output
    assert mock_run.call_args.kwargs["oauth_client_id"] == "cid"


def test_serve_oauth_env_precedence(monkeypatch):
    """AGENT_WINDOWS_OAUTH_CLIENT_ID env satisfies the guard."""
    from typer.testing import CliRunner
    from agent_windows.cli.commands import mcp_cmd
    monkeypatch.setenv("AGENT_WINDOWS_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("AGENT_WINDOWS_OAUTH_CLIENT_SECRET", "sec")
    with patch("agent_windows.mcp.run") as mock_run, \
         patch.object(mcp_cmd, "load_config", return_value={}):
        result = CliRunner().invoke(mcp_cmd.app, [
            "serve", "--transport", "streamable-http", "--host", "0.0.0.0",
            "--port", "9000"])
    assert result.exit_code == 0, result.output
    assert mock_run.call_args.kwargs["oauth_client_id"] == "cid"


def test_serve_oauth_mutual_exclusion_rejects_id_only():
    """Only --oauth-client-id (no secret) → BadParameter, run() not called."""
    from typer.testing import CliRunner
    from agent_windows.cli.commands import mcp_cmd
    with patch("agent_windows.mcp.run") as mock_run, \
         patch.object(mcp_cmd, "load_config", return_value={}):
        result = CliRunner().invoke(mcp_cmd.app, [
            "serve", "--transport", "streamable-http", "--host", "127.0.0.1",
            "--port", "9000", "--oauth-client-id", "cid"])
    assert result.exit_code != 0
    assert "both" in result.output.lower()
    mock_run.assert_not_called()


def test_serve_oauth_satisfies_bare_bind_guard():
    """Non-loopback bind with OAuth configured does NOT abort."""
    from typer.testing import CliRunner
    from agent_windows.cli.commands import mcp_cmd
    with patch("agent_windows.mcp.run") as mock_run, \
         patch.object(mcp_cmd, "load_config", return_value={}):
        result = CliRunner().invoke(mcp_cmd.app, [
            "serve", "--transport", "streamable-http", "--host", "0.0.0.0",
            "--port", "9000", "--oauth-client-id", "cid",
            "--oauth-client-secret", "sec"])
    assert result.exit_code == 0, result.output
    mock_run.assert_called_once()


def test_serve_oauth_ignored_on_stdio():
    """stdio transport ignores OAuth flags."""
    from typer.testing import CliRunner
    from agent_windows.cli.commands import mcp_cmd
    with patch("agent_windows.mcp.run") as mock_run, \
         patch.object(mcp_cmd, "load_config", return_value={}):
        result = CliRunner().invoke(mcp_cmd.app, [
            "serve", "--transport", "stdio",
            "--oauth-client-id", "cid", "--oauth-client-secret", "sec"])
    assert result.exit_code == 0, result.output
    assert mock_run.call_args.kwargs["oauth_client_id"] is None
    assert mock_run.call_args.kwargs["oauth_client_secret"] is None


# ── compute_allowed_hosts (DNS rebinding protection) ──────────────────

def test_compute_allowed_hosts_loopback_returns_three_hosts():
    """A loopback bind allows localhost, 127.0.0.1 and [::1]."""
    from agent_windows.mcp.server import compute_allowed_hosts
    expected = ["localhost", "127.0.0.1", "[::1]"]
    for host in ("localhost", "127.0.0.1", "127.1.2.3", "::1"):
        assert compute_allowed_hosts(host) == expected


def test_compute_allowed_hosts_named_host_returns_singleton():
    """A named (non-wildcard) host allows only that host."""
    from agent_windows.mcp.server import compute_allowed_hosts
    assert compute_allowed_hosts("myhost") == ["myhost"]
    assert compute_allowed_hosts("192.168.1.5") == ["192.168.1.5"]


def test_compute_allowed_hosts_wildcard_addresses_return_none():
    """A wildcard bind can't name a trusted set, so Host validation is skipped."""
    from agent_windows.mcp.server import compute_allowed_hosts
    for host in ("0.0.0.0", "::", ""):
        assert compute_allowed_hosts(host) is None


def test_compute_allowed_hosts_allow_insecure_remote_skips_even_loopback():
    """--allow-insecure-remote opts out of Host validation regardless of host."""
    from agent_windows.mcp.server import compute_allowed_hosts
    for host in ("localhost", "127.0.0.1", "myhost"):
        assert compute_allowed_hosts(host, allow_insecure_remote=True) is None


def test_run_http_mounts_trustedhost_when_allowed_hosts_set():
    """run() wires TrustedHostMiddleware (after Options) when allowed_hosts given."""
    from agent_windows.mcp import server
    from agent_windows.mcp.server import OptionsMiddleware
    from starlette.middleware.trustedhost import TrustedHostMiddleware
    fake = MagicMock()
    with patch.object(server, "build_server", return_value=fake):
        server.run(transport="streamable-http", host="127.0.0.1", port=9000,
                   allowed_hosts=["localhost", "127.0.0.1", "[::1]"])
    kinds = [mw.cls for mw in fake.run.call_args.kwargs["middleware"]]
    assert TrustedHostMiddleware in kinds
    # Ordering: Options first (always outermost), TrustedHost right after it —
    # preflight clears before the Host check, and a legit cross-origin preflight's
    # Host is always the target server so it is never wrongly rejected.
    assert kinds[0] is OptionsMiddleware
    assert kinds[1] is TrustedHostMiddleware


def test_run_full_middleware_ordering():
    """All five middlewares assemble in the documented order when configured.

    Options → TrustedHost → CORS → IPAllowlist → AuthKey. Pairwise tests leave
    the TrustedHost↔CORS adjacency unverified; this locks the whole chain.
    """
    from agent_windows.mcp import server
    from agent_windows.mcp.server import (OptionsMiddleware,
        IPAllowlistMiddleware, AuthKeyMiddleware)
    from starlette.middleware.trustedhost import TrustedHostMiddleware
    from starlette.middleware.cors import CORSMiddleware
    import ipaddress
    fake = MagicMock()
    with patch.object(server, "build_server", return_value=fake):
        server.run(transport="streamable-http", host="127.0.0.1", port=9000,
                   auth_key="k", cors_origins=["https://x.example.com"],
                   ip_allowlist=[ipaddress.ip_network("10.0.0.0/8")],
                   allowed_hosts=["localhost", "127.0.0.1", "[::1]"])
    kinds = [mw.cls for mw in fake.run.call_args.kwargs["middleware"]]
    assert kinds == [OptionsMiddleware, TrustedHostMiddleware,
                     CORSMiddleware, IPAllowlistMiddleware, AuthKeyMiddleware]


def test_run_http_omits_trustedhost_when_allowed_hosts_none():
    """No allowed_hosts → no TrustedHostMiddleware (only Options remains)."""
    from agent_windows.mcp import server
    from agent_windows.mcp.server import OptionsMiddleware
    from starlette.middleware.trustedhost import TrustedHostMiddleware
    fake = MagicMock()
    with patch.object(server, "build_server", return_value=fake):
        server.run(transport="streamable-http", host="0.0.0.0", port=9000,
                   allowed_hosts=None)
    kinds = [mw.cls for mw in fake.run.call_args.kwargs["middleware"]]
    assert TrustedHostMiddleware not in kinds
    assert kinds == [OptionsMiddleware]


def test_serve_forwards_allowed_hosts_for_loopback_http_bind():
    """serve() computes the loopback host set and forwards it to run()."""
    from typer.testing import CliRunner
    from agent_windows.cli.commands import mcp_cmd
    with patch("agent_windows.mcp.run") as mock_run, \
         patch.object(mcp_cmd, "load_config", return_value={}):
        CliRunner().invoke(mcp_cmd.app,
                           ["serve", "--transport", "streamable-http",
                            "--host", "127.0.0.1"])
    assert mock_run.call_args.kwargs["allowed_hosts"] == [
        "localhost", "127.0.0.1", "[::1]"]


def test_serve_forwards_allowed_hosts_for_named_http_bind():
    """A named host bind forwards [host] as the allowed_hosts set."""
    from typer.testing import CliRunner
    from agent_windows.cli.commands import mcp_cmd
    # auth_key comes from config (there's no --auth-key flag); it's needed so the
    # bare-bind guard allows the non-loopback named host.
    with patch("agent_windows.mcp.run") as mock_run, \
         patch.object(mcp_cmd, "load_config",
                      return_value={"mcp": {"auth_key": "k"}}):
        CliRunner().invoke(mcp_cmd.app,
                           ["serve", "--transport", "streamable-http",
                            "--host", "myhost"])
    assert mock_run.call_args.kwargs["allowed_hosts"] == ["myhost"]


def test_serve_forwards_none_allowed_hosts_for_wildcard_bind():
    """A 0.0.0.0 bind forwards None (can't name a trusted host set)."""
    from typer.testing import CliRunner
    from agent_windows.cli.commands import mcp_cmd
    with patch("agent_windows.mcp.run") as mock_run, \
         patch.object(mcp_cmd, "load_config",
                      return_value={"mcp": {"auth_key": "k"}}):
        CliRunner().invoke(mcp_cmd.app,
                           ["serve", "--transport", "streamable-http",
                            "--host", "0.0.0.0"])
    assert mock_run.call_args.kwargs["allowed_hosts"] is None


def test_serve_forwards_none_allowed_hosts_on_stdio():
    """stdio never forwards allowed_hosts (local IPC, no Host header)."""
    from typer.testing import CliRunner
    from agent_windows.cli.commands import mcp_cmd
    with patch("agent_windows.mcp.run") as mock_run, \
         patch.object(mcp_cmd, "load_config", return_value={}):
        CliRunner().invoke(mcp_cmd.app, ["serve", "--transport", "stdio"])
    assert mock_run.call_args.kwargs["allowed_hosts"] is None


def test_serve_allow_insecure_remote_forwards_none_allowed_hosts():
    """--allow-insecure-remote skips Host validation even for a loopback bind."""
    from typer.testing import CliRunner
    from agent_windows.cli.commands import mcp_cmd
    with patch("agent_windows.mcp.run") as mock_run, \
         patch.object(mcp_cmd, "load_config", return_value={}):
        CliRunner().invoke(mcp_cmd.app,
                           ["serve", "--transport", "streamable-http",
                            "--host", "127.0.0.1", "--allow-insecure-remote"])
    assert mock_run.call_args.kwargs["allowed_hosts"] is None


# ── run() / serve() ip_allowlist wiring ────────────────────────────────

def test_run_http_forwards_ip_allowlist_as_middleware():
    from agent_windows.mcp import server
    from agent_windows.mcp.server import IPAllowlistMiddleware, AuthKeyMiddleware
    import ipaddress
    fake = MagicMock()
    nets = [ipaddress.ip_network("10.0.0.0/8")]
    # Pass both ip_allowlist and auth_key so both middlewares are assembled and
    # we can assert ordering: OptionsMiddleware (always outermost), then
    # IPAllowlistMiddleware, then AuthKeyMiddleware — a blocked IP never reaches
    # the auth challenge.
    with patch.object(server, "build_server", return_value=fake):
        server.run(transport="streamable-http", host="127.0.0.1", port=9000,
                   auth_key="k", ip_allowlist=nets)
    kwargs = fake.run.call_args.kwargs
    kinds = [mw.cls for mw in kwargs["middleware"]]
    assert kinds[1:] == [IPAllowlistMiddleware, AuthKeyMiddleware]


def test_mcp_serve_forwards_ip_allowlist_flag():
    from typer.testing import CliRunner
    from agent_windows.cli.commands import mcp_cmd
    import ipaddress

    runner = CliRunner()
    with patch("agent_windows.mcp.run") as mock_run, \
         patch.object(mcp_cmd, "load_config", return_value={}):
        result = runner.invoke(mcp_cmd.app,
                               ["serve", "--transport", "streamable-http",
                                "--host", "127.0.0.1", "--port", "9000",
                                "--ip-allowlist", "10.0.0.0/8,192.168.1.5"])
    assert result.exit_code == 0, result.output
    nets = mock_run.call_args.kwargs["ip_allowlist"]
    # value-level check: bare IP became /32, CIDR passed through
    assert nets == [ipaddress.ip_network("10.0.0.0/8"),
                    ipaddress.ip_network("192.168.1.5/32")]


def test_mcp_serve_rejects_invalid_ip_allowlist():
    from typer.testing import CliRunner
    from agent_windows.cli.commands import mcp_cmd

    runner = CliRunner()
    with patch("agent_windows.mcp.run") as mock_run, \
         patch.object(mcp_cmd, "load_config", return_value={}):
        result = runner.invoke(mcp_cmd.app,
                               ["serve", "--transport", "streamable-http",
                                "--host", "127.0.0.1", "--port", "9000",
                                "--ip-allowlist", "not-an-ip"])
    assert result.exit_code != 0
    assert "allowlist" in result.output.lower()
    mock_run.assert_not_called()


def test_mcp_serve_reads_ip_allowlist_from_config():
    from typer.testing import CliRunner
    from agent_windows.cli.commands import mcp_cmd
    import ipaddress

    runner = CliRunner()
    with patch("agent_windows.mcp.run") as mock_run, \
         patch.object(mcp_cmd, "load_config",
                      return_value={"security": {"ip_allowlist": ["10.0.0.0/8"]}}):
        result = runner.invoke(mcp_cmd.app,
                               ["serve", "--transport", "streamable-http",
                                "--host", "127.0.0.1", "--port", "9000"])
    assert result.exit_code == 0, result.output
    assert mock_run.call_args.kwargs["ip_allowlist"] == [
        ipaddress.ip_network("10.0.0.0/8")]


def test_mcp_serve_reads_ip_allowlist_from_env(monkeypatch):
    """AGENT_WINDOWS_IP_ALLOWLIST sits between the flag and config in precedence."""
    from typer.testing import CliRunner
    from agent_windows.cli.commands import mcp_cmd
    import ipaddress

    monkeypatch.setenv("AGENT_WINDOWS_IP_ALLOWLIST", "172.16.0.0/12")
    runner = CliRunner()
    with patch("agent_windows.mcp.run") as mock_run, \
         patch.object(mcp_cmd, "load_config",
                      return_value={"security": {"ip_allowlist": ["10.0.0.0/8"]}}):
        result = runner.invoke(mcp_cmd.app,
                               ["serve", "--transport", "streamable-http",
                                "--host", "127.0.0.1", "--port", "9000"])
    assert result.exit_code == 0, result.output
    # env wins over config
    assert mock_run.call_args.kwargs["ip_allowlist"] == [
        ipaddress.ip_network("172.16.0.0/12")]


def test_mcp_serve_ip_allowlist_flag_overrides_env_and_config(monkeypatch):
    """The --ip-allowlist flag takes precedence over env and config."""
    from typer.testing import CliRunner
    from agent_windows.cli.commands import mcp_cmd
    import ipaddress

    monkeypatch.setenv("AGENT_WINDOWS_IP_ALLOWLIST", "172.16.0.0/12")
    runner = CliRunner()
    with patch("agent_windows.mcp.run") as mock_run, \
         patch.object(mcp_cmd, "load_config",
                      return_value={"security": {"ip_allowlist": ["10.0.0.0/8"]}}):
        result = runner.invoke(mcp_cmd.app,
                               ["serve", "--transport", "streamable-http",
                                "--host", "127.0.0.1", "--port", "9000",
                                "--ip-allowlist", "192.168.0.0/16"])
    assert result.exit_code == 0, result.output
    assert mock_run.call_args.kwargs["ip_allowlist"] == [
        ipaddress.ip_network("192.168.0.0/16")]


def test_mcp_serve_empty_ip_allowlist_flag_falls_through_to_config(monkeypatch):
    """An empty --ip-allowlist value is treated as unset and falls to config.

    Pinning the chosen behavior: empty does not mean "disable" — it means "no
    value here, consult the next-lower precedence layer."
    """
    from typer.testing import CliRunner
    from agent_windows.cli.commands import mcp_cmd
    import ipaddress

    monkeypatch.delenv("AGENT_WINDOWS_IP_ALLOWLIST", raising=False)
    runner = CliRunner()
    with patch("agent_windows.mcp.run") as mock_run, \
         patch.object(mcp_cmd, "load_config",
                      return_value={"security": {"ip_allowlist": ["10.0.0.0/8"]}}):
        result = runner.invoke(mcp_cmd.app,
                               ["serve", "--transport", "streamable-http",
                                "--host", "127.0.0.1", "--port", "9000",
                                "--ip-allowlist", ""])
    assert result.exit_code == 0, result.output
    assert mock_run.call_args.kwargs["ip_allowlist"] == [
        ipaddress.ip_network("10.0.0.0/8")]


def test_mcp_serve_config_scalar_list_value_is_normalized():
    """`config set` writes scalars, so a list key may land as a bare string.
    The serve reads must wrap a scalar into a one-element list rather than
    iterating its characters (which would corrupt parsing). Regression test for
    the config-set round-trip across all four list keys.
    """
    from typer.testing import CliRunner
    from agent_windows.cli.commands import mcp_cmd
    import ipaddress

    runner = CliRunner()
    with patch("agent_windows.mcp.run") as mock_run, \
         patch.object(mcp_cmd, "load_config", return_value={
             "security": {"ip_allowlist": "10.0.0.0/8",      # scalar, not a list
                          "cors_origins": "https://c.example.com"},
             "tools": {"include": "screenshot", "exclude": "shell"}}):
        result = runner.invoke(mcp_cmd.app,
                               ["serve", "--transport", "streamable-http",
                                "--host", "127.0.0.1", "--port", "9000"])
    assert result.exit_code == 0, result.output
    kwargs = mock_run.call_args.kwargs
    # scalar "10.0.0.0/8" parsed as one network, not 10 char-fragments
    assert kwargs["ip_allowlist"] == [ipaddress.ip_network("10.0.0.0/8")]
    assert kwargs["cors_origins"] == ["https://c.example.com"]
    assert kwargs["tools"] == ["screenshot"]
    assert kwargs["exclude_tools"] == ["shell"]


# ── OptionsMiddleware + CORS wiring ────────────────────────────────────

def _run_options_middleware(scope, allowed_origins):
    """Drive OptionsMiddleware; return (downstream_called, sent)."""
    from agent_windows.mcp.server import OptionsMiddleware
    return _drive(lambda app: OptionsMiddleware(app, allowed_origins=allowed_origins),
                  scope)


def _options_scope(origin=None):
    """An OPTIONS preflight scope with optional Origin header."""
    headers = {}
    if origin is not None:
        headers["origin"] = origin
    return _http_scope(headers=headers) | {"method": "OPTIONS"}


def test_options_middleware_empty_allowlist_emits_no_cors_headers():
    # No allowlist → 200 OK with no access-control-* headers (SOP blocks).
    called, sent = _run_options_middleware(_options_scope("https://x.example.com"), [])
    assert called is False                       # preflight handled, not passed down
    start = sent[0]
    assert start["status"] == 200
    header_names = [h[0] for h in start["headers"]]
    assert not any(n.startswith(b"access-control") for n in header_names)


def test_options_middleware_matching_origin_emits_cors_headers():
    called, sent = _run_options_middleware(
        _options_scope("https://client.example.com"), ["https://client.example.com"])
    assert called is False
    start = sent[0]
    assert start["status"] == 200
    hdrs = dict(start["headers"])
    assert hdrs[b"access-control-allow-origin"] == b"https://client.example.com"
    assert hdrs[b"access-control-allow-methods"] == b"GET, POST, OPTIONS"
    assert b"authorization" in hdrs[b"access-control-allow-headers"]
    assert hdrs[b"vary"] == b"Origin"


def test_options_middleware_non_matching_origin_emits_no_cors_headers():
    # Allowlist set but Origin not in it → bare 200, browser still blocks.
    called, sent = _run_options_middleware(
        _options_scope("https://evil.example.com"), ["https://client.example.com"])
    assert called is False
    assert sent[0]["status"] == 200
    header_names = [h[0] for h in sent[0]["headers"]]
    assert not any(n.startswith(b"access-control") for n in header_names)


def test_options_middleware_passes_through_non_options():
    # A GET must reach the downstream app untouched.
    scope = _http_scope() | {"method": "GET"}
    called, sent = _run_options_middleware(scope, ["https://client.example.com"])
    assert called is True
    assert sent == []


def test_run_http_cors_configured_adds_both_middlewares():
    from agent_windows.mcp import server
    from agent_windows.mcp.server import OptionsMiddleware
    from starlette.middleware.cors import CORSMiddleware
    fake = MagicMock()
    with patch.object(server, "build_server", return_value=fake):
        server.run(transport="streamable-http", host="127.0.0.1", port=9000,
                   cors_origins=["https://client.example.com"])
    kinds = [mw.cls for mw in fake.run.call_args.kwargs["middleware"]]
    # OptionsMiddleware (outermost, always) then CORSMiddleware — ordering is
    # load-bearing: preflight must be answered before any auth/CORS gate, and
    # CORSMiddleware decorates the actual (non-preflight) responses.
    assert kinds[:2] == [OptionsMiddleware, CORSMiddleware]


def test_mcp_serve_forwards_cors_origins_flag():
    from typer.testing import CliRunner
    from agent_windows.cli.commands import mcp_cmd

    runner = CliRunner()
    with patch("agent_windows.mcp.run") as mock_run, \
         patch.object(mcp_cmd, "load_config", return_value={}):
        result = runner.invoke(mcp_cmd.app,
                               ["serve", "--transport", "streamable-http",
                                "--host", "127.0.0.1", "--port", "9000",
                                "--cors-origins", "https://a.example.com,https://b.example.com"])
    assert result.exit_code == 0, result.output
    assert mock_run.call_args.kwargs["cors_origins"] == [
        "https://a.example.com", "https://b.example.com"]


def test_mcp_serve_reads_cors_origins_from_config():
    from typer.testing import CliRunner
    from agent_windows.cli.commands import mcp_cmd

    runner = CliRunner()
    with patch("agent_windows.mcp.run") as mock_run, \
         patch.object(mcp_cmd, "load_config",
                      return_value={"security": {"cors_origins": ["https://c.example.com"]}}):
        result = runner.invoke(mcp_cmd.app,
                               ["serve", "--transport", "streamable-http",
                                "--host", "127.0.0.1", "--port", "9000"])
    assert result.exit_code == 0, result.output
    assert mock_run.call_args.kwargs["cors_origins"] == ["https://c.example.com"]


def test_mcp_serve_reads_cors_origins_from_env(monkeypatch):
    from typer.testing import CliRunner
    from agent_windows.cli.commands import mcp_cmd

    monkeypatch.setenv("AGENT_WINDOWS_CORS_ORIGINS", "https://env.example.com")
    runner = CliRunner()
    with patch("agent_windows.mcp.run") as mock_run, \
         patch.object(mcp_cmd, "load_config",
                      return_value={"security": {"cors_origins": ["https://c.example.com"]}}):
        result = runner.invoke(mcp_cmd.app,
                               ["serve", "--transport", "streamable-http",
                                "--host", "127.0.0.1", "--port", "9000"])
    assert result.exit_code == 0, result.output
    # env wins over config
    assert mock_run.call_args.kwargs["cors_origins"] == ["https://env.example.com"]


# ── apply_tool_filter + tool filtering wiring ──────────────────────────

def _registered_tool_names(mcp):
    """Read the current registered tool names from a FastMCP instance."""
    comps = mcp._local_provider._components
    return {getattr(v, "name", None) or k.split(":", 1)[1].split("@", 1)[0]
            for k, v in comps.items()
            if isinstance(k, str) and k.startswith("tool:")}


def test_apply_tool_filter_exclude_removes_named_tools():
    from agent_windows.mcp.server import build_server, reset_server, apply_tool_filter
    reset_server()
    mcp = build_server()
    before = _registered_tool_names(mcp)
    assert "shell" in before and "registry" in before
    apply_tool_filter(mcp, explicit_tools=None, exclude_tools=["shell", "registry"])
    after = _registered_tool_names(mcp)
    assert "shell" not in after and "registry" not in after
    # the rest remain
    assert (before - {"shell", "registry"}) <= after
    reset_server()


def test_apply_tool_filter_explicit_keeps_only_whitelist():
    from agent_windows.mcp.server import build_server, reset_server, apply_tool_filter
    reset_server()
    mcp = build_server()
    apply_tool_filter(mcp, explicit_tools=["screenshot", "snapshot"],
                      exclude_tools=None)
    after = _registered_tool_names(mcp)
    assert after == {"screenshot", "snapshot"}
    reset_server()


def test_apply_tool_filter_explicit_overrides_exclude():
    from agent_windows.mcp.server import build_server, reset_server, apply_tool_filter
    reset_server()
    mcp = build_server()
    # explicit wins: even though exclude lists screenshot, explicit keeps it
    apply_tool_filter(mcp, explicit_tools=["screenshot"],
                      exclude_tools=["screenshot", "shell"])
    after = _registered_tool_names(mcp)
    assert after == {"screenshot"}
    reset_server()


def test_apply_tool_filter_ignores_unknown_names():
    from agent_windows.mcp.server import build_server, reset_server, apply_tool_filter
    reset_server()
    mcp = build_server()
    before = _registered_tool_names(mcp)
    # unknown names are silently ignored, not an error
    apply_tool_filter(mcp, explicit_tools=None, exclude_tools=["does_not_exist"])
    assert _registered_tool_names(mcp) == before
    reset_server()


def test_mcp_serve_forwards_exclude_tools_flag():
    from typer.testing import CliRunner
    from agent_windows.cli.commands import mcp_cmd

    runner = CliRunner()
    with patch("agent_windows.mcp.run") as mock_run, \
         patch.object(mcp_cmd, "load_config", return_value={}):
        result = runner.invoke(mcp_cmd.app,
                               ["serve", "--transport", "stdio",
                                "--exclude-tools", "shell,registry"])
    assert result.exit_code == 0, result.output
    assert mock_run.call_args.kwargs["exclude_tools"] == ["shell", "registry"]


def test_mcp_serve_forwards_tools_flag():
    from typer.testing import CliRunner
    from agent_windows.cli.commands import mcp_cmd

    runner = CliRunner()
    with patch("agent_windows.mcp.run") as mock_run, \
         patch.object(mcp_cmd, "load_config", return_value={}):
        result = runner.invoke(mcp_cmd.app,
                               ["serve", "--transport", "stdio",
                                "--tools", "screenshot,snapshot"])
    assert result.exit_code == 0, result.output
    assert mock_run.call_args.kwargs["tools"] == ["screenshot", "snapshot"]


def test_mcp_serve_reads_exclude_tools_from_config():
    from typer.testing import CliRunner
    from agent_windows.cli.commands import mcp_cmd

    runner = CliRunner()
    with patch("agent_windows.mcp.run") as mock_run, \
         patch.object(mcp_cmd, "load_config",
                      return_value={"tools": {"exclude": ["shell"]}}):
        result = runner.invoke(mcp_cmd.app, ["serve", "--transport", "stdio"])
    assert result.exit_code == 0, result.output
    assert mock_run.call_args.kwargs["exclude_tools"] == ["shell"]


def test_mcp_serve_reads_tools_from_env(monkeypatch):
    from typer.testing import CliRunner
    from agent_windows.cli.commands import mcp_cmd

    monkeypatch.setenv("AGENT_WINDOWS_TOOLS", "screenshot,click")
    runner = CliRunner()
    with patch("agent_windows.mcp.run") as mock_run, \
         patch.object(mcp_cmd, "load_config", return_value={}):
        result = runner.invoke(mcp_cmd.app, ["serve", "--transport", "stdio"])
    assert result.exit_code == 0, result.output
    assert mock_run.call_args.kwargs["tools"] == ["screenshot", "click"]


def test_mcp_serve_tools_env_overrides_config(monkeypatch):
    """AGENT_WINDOWS_TOOLS beats [tools] include in config."""
    from typer.testing import CliRunner
    from agent_windows.cli.commands import mcp_cmd

    monkeypatch.setenv("AGENT_WINDOWS_TOOLS", "screenshot")
    runner = CliRunner()
    with patch("agent_windows.mcp.run") as mock_run, \
         patch.object(mcp_cmd, "load_config",
                      return_value={"tools": {"include": ["shell", "registry"]}}):
        result = runner.invoke(mcp_cmd.app, ["serve", "--transport", "stdio"])
    assert result.exit_code == 0, result.output
    # env wins over config
    assert mock_run.call_args.kwargs["tools"] == ["screenshot"]


def test_mcp_serve_tools_flag_overrides_env_and_config(monkeypatch):
    """--tools takes precedence over both env and config."""
    from typer.testing import CliRunner
    from agent_windows.cli.commands import mcp_cmd

    monkeypatch.setenv("AGENT_WINDOWS_TOOLS", "shell")
    runner = CliRunner()
    with patch("agent_windows.mcp.run") as mock_run, \
         patch.object(mcp_cmd, "load_config",
                      return_value={"tools": {"include": ["registry"]}}):
        result = runner.invoke(mcp_cmd.app,
                               ["serve", "--transport", "stdio",
                                "--tools", "screenshot,snapshot"])
    assert result.exit_code == 0, result.output
    assert mock_run.call_args.kwargs["tools"] == ["screenshot", "snapshot"]


def test_run_invokes_apply_tool_filter_when_tools_given():
    """run() must call apply_tool_filter when tools/exclude_tools are set.

    The other run() tests patch build_server with a MagicMock, so the filter
    guard never executes against the real registry — this covers that seam.
    """
    from agent_windows.mcp import server
    fake = MagicMock()
    with patch.object(server, "build_server", return_value=fake), \
         patch.object(server, "apply_tool_filter") as af:
        server.run(transport="stdio", tools=["screenshot"],
                   exclude_tools=["shell"])
    af.assert_called_once_with(fake, ["screenshot"], ["shell"])


def test_run_skips_apply_tool_filter_when_no_filter():
    """No tools/exclude_tools → apply_tool_filter is never called."""
    from agent_windows.mcp import server
    fake = MagicMock()
    with patch.object(server, "build_server", return_value=fake), \
         patch.object(server, "apply_tool_filter") as af:
        server.run(transport="stdio")
    af.assert_not_called()


# ── stateless-http mode ────────────────────────────────────────────────

def test_run_http_forwards_stateless_http():
    from agent_windows.mcp import server
    fake = MagicMock()
    with patch.object(server, "build_server", return_value=fake):
        server.run(transport="streamable-http", host="127.0.0.1", port=9000,
                   stateless_http=True)
    assert fake.run.call_args.kwargs["stateless_http"] is True


def test_run_http_defaults_stateless_http_false():
    from agent_windows.mcp import server
    fake = MagicMock()
    with patch.object(server, "build_server", return_value=fake):
        server.run(transport="streamable-http", host="127.0.0.1", port=9000)
    assert fake.run.call_args.kwargs["stateless_http"] is False


def test_mcp_serve_stateless_http_flag():
    from typer.testing import CliRunner
    from agent_windows.cli.commands import mcp_cmd

    runner = CliRunner()
    with patch("agent_windows.mcp.run") as mock_run, \
         patch.object(mcp_cmd, "load_config", return_value={}):
        result = runner.invoke(mcp_cmd.app,
                               ["serve", "--transport", "streamable-http",
                                "--host", "127.0.0.1", "--port", "9000",
                                "--stateless-http"])
    assert result.exit_code == 0, result.output
    assert mock_run.call_args.kwargs["stateless_http"] is True


def test_mcp_serve_reads_stateless_http_from_config():
    from typer.testing import CliRunner
    from agent_windows.cli.commands import mcp_cmd

    runner = CliRunner()
    with patch("agent_windows.mcp.run") as mock_run, \
         patch.object(mcp_cmd, "load_config",
                      return_value={"mcp": {"stateless_http": True}}):
        result = runner.invoke(mcp_cmd.app,
                               ["serve", "--transport", "streamable-http",
                                "--host", "127.0.0.1", "--port", "9000"])
    assert result.exit_code == 0, result.output
    assert mock_run.call_args.kwargs["stateless_http"] is True


def test_mcp_serve_stateless_http_from_env(monkeypatch):
    from typer.testing import CliRunner
    from agent_windows.cli.commands import mcp_cmd

    monkeypatch.setenv("AGENT_WINDOWS_STATELESS_HTTP", "true")
    runner = CliRunner()
    with patch("agent_windows.mcp.run") as mock_run, \
         patch.object(mcp_cmd, "load_config", return_value={}):
        result = runner.invoke(mcp_cmd.app,
                               ["serve", "--transport", "streamable-http",
                                "--host", "127.0.0.1", "--port", "9000"])
    assert result.exit_code == 0, result.output
    assert mock_run.call_args.kwargs["stateless_http"] is True


def test_run_stdio_with_stateless_http_does_not_forward_to_mcp_run():
    """stateless_http must never reach the stdio mcp.run (FastMCP's stdio run
    rejects it). Belt-and-suspenders: run() always computes it, but the stdio
    branch's mcp.run call must omit the kwarg entirely."""
    from agent_windows.mcp import server
    fake = MagicMock()
    with patch.object(server, "build_server", return_value=fake):
        server.run(transport="stdio", stateless_http=True)
    kwargs = fake.run.call_args.kwargs
    assert kwargs["transport"] == "stdio"
    assert "stateless_http" not in kwargs


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
