"""Tests for the MCP server builder and run() transport dispatch."""
from unittest.mock import patch, MagicMock

import pytest


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
