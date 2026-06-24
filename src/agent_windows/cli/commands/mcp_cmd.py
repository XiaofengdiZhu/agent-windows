"""`agent-windows mcp` — run the MCP server.

This is a Typer subcommand group (like `config` / `daemon`). The launcher
delegates `mcp` to the Python CLI verbatim (alongside `config`), so
`agent-windows mcp serve` reaches this module unchanged.

`serve` starts the FastMCP server, which auto-starts the daemon on first tool
call — no separate `daemon start` needed. Defaults come from the `[mcp]` config
section (set via `mcp auth`); explicit flags override them. auth_key and TLS
only apply to HTTP transports (sse / streamable-http), never stdio.
"""
import os
import secrets
import subprocess
import sys
from pathlib import Path

import typer

from agent_windows.mcp.server import is_loopback_host, parse_ip_allowlist
from agent_windows.utils.config import load_config

app = typer.Typer(help="MCP server", no_args_is_help=True)


def _parse_allowlist(entries):
    """Parse raw IP/CIDR string entries into networks, or None when empty.

    Returns None (no middleware) for an empty list; raises typer.BadParameter
    for invalid entries so the user gets a clear CLI error instead of a buried
    ValueError.
    """
    if not entries:
        return None
    try:
        return parse_ip_allowlist(entries)
    except ValueError as exc:
        raise typer.BadParameter(f"Invalid IP allowlist: {exc}", param_hint="--ip-allowlist")


@app.command()
def serve(
    transport: str = typer.Option(
        None, "--transport",
        help="Transport: stdio (default, local clients) | sse | streamable-http"),
    host: str = typer.Option(
        None, "--host", help="Host to bind (sse/streamable-http)"),
    port: int = typer.Option(
        None, "--port", help="Port to bind (sse/streamable-http)"),
    debug: bool = typer.Option(False, "--debug", help="Verbose logging"),
    allow_insecure_remote: bool = typer.Option(
        False, "--allow-insecure-remote",
        help="Allow an unauthenticated HTTP bind to a non-loopback host (NOT recommended)"),
    ip_allowlist: str = typer.Option(
        None, "--ip-allowlist",
        help="Comma-separated client IPs/CIDRs to allow on HTTP (e.g. '10.0.0.0/8,192.168.1.5'). "
             "Env: AGENT_WINDOWS_IP_ALLOWLIST"),
) -> None:
    """Run the agent-windows MCP server.

    Defaults come from the [mcp] config section; explicit flags override.
    auth_key/TLS are read from config and applied only to HTTP transports.
    """
    full_cfg = load_config()
    cfg = full_cfg.get("mcp") or {}
    security_cfg = full_cfg.get("security") or {}
    transport = transport or cfg.get("transport", "stdio")
    host = host or cfg.get("host", "127.0.0.1")
    port = port if port is not None else int(cfg.get("port", 8000))
    if not 1 <= port <= 65535:
        # Fail fast with a clear message rather than letting uvicorn raise an
        # opaque bind error. Applies to both the --port flag and [mcp] port.
        raise typer.BadParameter(
            f"port must be between 1 and 65535 (got {port})", param_hint="--port")

    # auth_key / TLS are HTTP-only — stdio ignores them (local IPC, no network).
    if transport == "stdio":
        auth_key = None
        ssl_certfile = None
        ssl_keyfile = None
        parsed_allowlist = None
    else:
        auth_key = cfg.get("auth_key") or None
        ssl_certfile = cfg.get("ssl_certfile") or None
        ssl_keyfile = cfg.get("ssl_keyfile") or None
        # IP allowlist precedence: --ip-allowlist flag > AGENT_WINDOWS_IP_ALLOWLIST
        # env > [security] ip_allowlist config. Comma-separated; stdio ignores it.
        raw_allowlist = ip_allowlist
        if raw_allowlist is None:
            raw_allowlist = os.environ.get("AGENT_WINDOWS_IP_ALLOWLIST")
        entries = ([e.strip() for e in raw_allowlist.split(",") if e.strip()]
                   if raw_allowlist else None)
        if not entries:
            entries = [e for e in (security_cfg.get("ip_allowlist") or []) if e]
        parsed_allowlist = _parse_allowlist(entries)

    # A bool flag needs a config fallback like host/port: the command-line flag
    # wins; otherwise [mcp] allow_insecure_remote (default false).
    allow_insecure_remote = allow_insecure_remote or bool(
        cfg.get("allow_insecure_remote", False))

    # Footgun guard: refuse a bare non-loopback HTTP bind. Binding 0.0.0.0 (or
    # any LAN/private address) with no auth_key exposes the full MCP toolset —
    # screenshots, shell, registry, file I/O — to anyone who can reach the port.
    # Require either a bearer token (set via `mcp auth`) or an explicit
    # --allow-insecure-remote opt-in. stdio is exempt (local IPC, no network
    # surface). OAuth is not in this build, so the only remedies are auth_key or
    # the explicit opt-in flag.
    if (transport != "stdio"
            and not is_loopback_host(host)
            and not auth_key
            and not allow_insecure_remote):
        raise typer.BadParameter(
            f"Refusing to bind HTTP transport to {host!r} without authentication.\n"
            "  Run `agent-windows mcp auth` to set a bearer token (auth_key),\n"
            "  or pass --allow-insecure-remote to allow unauthenticated access (NOT recommended).",
            param_hint="--host")

    if allow_insecure_remote and transport != "stdio":
        typer.echo(
            f"WARNING: binding HTTP transport to {host!r} with NO authentication. "
            "Anyone who can reach this port has full desktop control. NOT recommended.",
            err=True)

    from agent_windows.mcp import run
    run(transport=transport, host=host, port=port, debug=debug,
        auth_key=auth_key, ssl_certfile=ssl_certfile, ssl_keyfile=ssl_keyfile,
        ip_allowlist=parsed_allowlist)


# ── `mcp auth` ──────────────────────────────────────────────────────────

def _gen_tls(host: str, cert_path, key_path) -> None:
    """Generate a TLS cert/key pair, preferring mkcert over openssl.

    Mirrors Windows-MCP's _gen_tls. mkcert yields a Windows-trusted cert;
    openssl yields a self-signed one (with import instructions printed).
    """
    cert_path, key_path = Path(cert_path), Path(key_path)
    mkcert = subprocess.run(["where", "mkcert"], capture_output=True).returncode == 0
    if mkcert:
        typer.echo("mkcert detected — generating a locally-trusted certificate...")
        if subprocess.run(["mkcert", "-install"], capture_output=True, text=True).returncode != 0:
            raise typer.BadParameter("mkcert -install failed")
        sans = [host] if host not in ("0.0.0.0", "") else ["localhost", "127.0.0.1", "::1"]
        r = subprocess.run(["mkcert", "-cert-file", str(cert_path),
                            "-key-file", str(key_path), *sans],
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise typer.BadParameter(f"mkcert failed: {r.stderr.strip()}")
        typer.echo("  Certificate is automatically trusted by Windows.")
    else:
        typer.echo("mkcert not found — falling back to openssl (self-signed)...")
        typer.echo("  Tip: winget install FiloSottile.mkcert  for auto-trusted certs next time.")
        r = subprocess.run(
            ["openssl", "req", "-x509", "-newkey", "rsa:4096",
             "-keyout", str(key_path), "-out", str(cert_path),
             "-days", "365", "-nodes", "-subj", f"/CN={host or 'agent-windows'}"],
            capture_output=True, text=True)
        if r.returncode != 0:
            raise typer.BadParameter(f"openssl failed: {r.stderr.strip()}")
        typer.echo('  To trust this cert, run in an elevated PowerShell:')
        typer.echo(f'    Import-Certificate -FilePath "{cert_path}" '
                   f'-CertStoreLocation Cert:\\LocalMachine\\Root')
    typer.echo(f"  cert → {cert_path}")
    typer.echo(f"  key  → {key_path}")


@app.command()
def auth(
    transport: str = typer.Option(
        "streamable-http", "--transport",
        help="Transport to configure: stdio | sse | streamable-http"),
    host: str = typer.Option(
        "0.0.0.0", "--host", help="Host the server will bind to"),
    port: int = typer.Option(
        8000, "--port", help="Port the server will bind to"),
    with_tls: bool = typer.Option(
        False, "--with-tls", help="Generate a TLS cert/key (mkcert or self-signed)"),
    force: bool = typer.Option(
        False, "--force", help="Overwrite an existing auth_key without prompting"),
) -> None:
    """Generate an auth key (and optionally TLS certs), save to [mcp], print client config."""
    from agent_windows.utils.config import load_config, save_config, get_data_dir

    cfg = load_config()
    existing = (cfg.get("mcp") or {}).get("auth_key", "")
    if existing and not force:
        typer.echo("Auth key already set. Use --force to regenerate.")
        return

    new_key = secrets.token_urlsafe(32)
    cfg.setdefault("mcp", {})
    cfg["mcp"]["auth_key"] = new_key
    cfg["mcp"]["transport"] = transport
    cfg["mcp"]["host"] = host
    cfg["mcp"]["port"] = port
    typer.echo(f"Generated auth key: {new_key}")

    if with_tls:
        if transport == "stdio":
            raise typer.BadParameter("TLS has no effect on the stdio transport.")
        data_dir = get_data_dir()
        cert_path = data_dir / "mcp-cert.pem"
        key_path = data_dir / "mcp-key.pem"
        _gen_tls(host, cert_path, key_path)
        cfg["mcp"]["ssl_certfile"] = str(cert_path)
        cfg["mcp"]["ssl_keyfile"] = str(key_path)

    save_config(cfg)
    from agent_windows.utils.config import get_config_path
    typer.echo(f"\nSaved to {get_config_path()}")

    # Print the ready-to-paste MCP client config snippet.
    if transport == "stdio":
        typer.echo("\n─── Claude Desktop config (stdio) ───")
        typer.echo("""\
{
  "mcpServers": {
    "agent-windows": {
      "command": "agent-windows",
      "args": ["mcp", "serve"]
    }
  }
}""")
        return

    scheme = "https" if with_tls else "http"
    url = f"{scheme}://{host}:{port}/mcp/"
    sse_url = f"{scheme}://{host}:{port}/sse"
    typer.echo("\n─── Start the server ───")
    typer.echo("  agent-windows mcp serve")
    if transport == "sse":
        typer.echo("\n─── Claude Desktop config (SSE) ───")
        typer.echo(f"""\
{{
  "mcpServers": {{
    "agent-windows": {{
      "type": "sse",
      "url": "{sse_url}",
      "headers": {{ "Authorization": "Bearer {new_key}" }}
    }}
  }}
}}""")
    else:
        typer.echo("\n─── Claude Desktop config (Streamable HTTP) ───")
        typer.echo(f"""\
{{
  "mcpServers": {{
    "agent-windows": {{
      "type": "http",
      "url": "{url}",
      "headers": {{ "Authorization": "Bearer {new_key}" }}
    }}
  }}
}}""")


# ── `mcp install` / `mcp uninstall` ─────────────────────────────────────

_TASK_NAME = "agent-windows-mcp"


def _start_script_path() -> Path:
    from agent_windows.utils.config import get_data_dir
    return get_data_dir() / "start-mcp.cmd"


def _resolve_program() -> list[str]:
    """argv prefix to invoke `agent-windows mcp serve` from Task Scheduler.

    Uses the running interpreter so the scheduled task always targets this
    exact installation, regardless of what's on PATH. Mirrors Windows-MCP's
    _resolve_program but for `python -m agent_windows` (agent-windows has no
    standalone console script that the scheduler can resolve reliably).
    """
    return [sys.executable, "-m", "agent_windows"]


def _build_start_script(program_args: list[str]) -> str:
    from agent_windows.utils.config import get_log_dir
    log_out = get_log_dir() / "mcp-server.log"
    log_err = get_log_dir() / "mcp-server.error.log"
    command = subprocess.list2cmdline(program_args)
    return f'@echo off\nsetlocal\n{command} 1>>"{log_out}" 2>>"{log_err}"\n'


def _schtasks(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["schtasks", *args], capture_output=True, text=True)


def _register_task_powershell(task_name: str, script_path: str) -> subprocess.CompletedProcess:
    """Register an ONLOGON scheduled task via PowerShell at RunLevel Limited.

    Unlike `schtasks /Create /SC ONLOGON`, Register-ScheduledTask with
    -RunLevel Limited does NOT require an elevated shell. Mirrors Windows-MCP.
    """
    ps = (
        f"$action  = New-ScheduledTaskAction -Execute '{script_path}';"
        f"$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME;"
        f"$set     = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries;"
        f"Register-ScheduledTask -TaskName '{task_name}' -Action $action"
        f" -Trigger $trigger -Settings $set -RunLevel Limited -Force"
    )
    return subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                          capture_output=True, text=True)


@app.command()
def install(
    transport: str = typer.Option(
        "streamable-http", "--transport",
        help="Transport for the background server (stdio not supported as a service)"),
    host: str = typer.Option("127.0.0.1", "--host", help="Host to bind"),
    port: int = typer.Option(8000, "--port", help="Port to bind"),
    force: bool = typer.Option(False, "--force", help="Reinstall even if already installed"),
) -> None:
    """Install the MCP server as a scheduled task that starts at login."""
    query = _schtasks("/Query", "/TN", _TASK_NAME)
    if query.returncode == 0 and not force:
        typer.echo(f"Scheduled task '{_TASK_NAME}' is already installed.")
        typer.echo("Use --force to reinstall.")
        return

    script_path = _start_script_path()
    exe = _resolve_program()
    args = exe + ["mcp", "serve", "--transport", transport,
                  "--host", host, "--port", str(port)]
    script_path.write_text(_build_start_script(args), encoding="utf-8")

    # Remove any existing task first when forcing a reinstall.
    _schtasks("/Delete", "/TN", _TASK_NAME, "/F")

    result = _register_task_powershell(_TASK_NAME, str(script_path))
    if result.returncode != 0:
        raise typer.BadParameter(
            f"Register-ScheduledTask failed:\n{result.stderr.strip() or result.stdout.strip()}")

    if _schtasks("/Run", "/TN", _TASK_NAME).returncode != 0:
        typer.echo("Warning: scheduled task registered but could not be started now.")

    typer.echo("Scheduled task installed — server is starting now.")
    typer.echo(f"  Task      : {_TASK_NAME}")
    typer.echo(f"  Transport : {transport}")
    typer.echo(f"  Address   : {host}:{port}")
    typer.echo("The server will restart automatically at every login.")
    typer.echo("Run `agent-windows mcp uninstall` to remove it.")


@app.command()
def uninstall() -> None:
    """Remove the scheduled task and stop the background MCP server."""
    if _schtasks("/End", "/TN", _TASK_NAME).returncode == 0:
        typer.echo("Stopped the running server.")
    if _schtasks("/Delete", "/TN", _TASK_NAME, "/F").returncode == 0:
        typer.echo(f"Removed scheduled task '{_TASK_NAME}'.")
    else:
        typer.echo("No scheduled task found.")
    script_path = _start_script_path()
    if script_path.exists():
        script_path.unlink()
        typer.echo(f"Removed {script_path}")
    typer.echo("The MCP server will no longer start at login.")
