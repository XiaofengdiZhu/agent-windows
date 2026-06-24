"""Config commands: init, show, set."""

import typer

from agent_windows.utils.config import (
    init_config,
    load_config,
    set_config_value,
    get_config_path,
    get_api_key,
)

app = typer.Typer(help="Configuration management")


@app.command("init")
def config_init() -> None:
    """Initialize config file with defaults."""
    path = init_config()
    typer.echo(f"Config initialized at: {path}")


@app.command("show")
def config_show() -> None:
    """Show current configuration."""
    import copy
    import toml

    config = load_config()
    display_config = copy.deepcopy(config)
    # Mask API key
    if "vlm" in display_config and "api_key" in display_config["vlm"]:
        key = display_config["vlm"]["api_key"]
        if key and len(key) > 8:
            display_config["vlm"]["api_key"] = key[:4] + "..." + key[-4:]
        elif key:
            display_config["vlm"]["api_key"] = "***"
    # Mask MCP auth key (same secret-class as the VLM api key)
    if "mcp" in display_config and "auth_key" in display_config["mcp"]:
        mkey = display_config["mcp"]["auth_key"]
        if mkey and len(mkey) > 8:
            display_config["mcp"]["auth_key"] = mkey[:4] + "..." + mkey[-4:]
        elif mkey:
            display_config["mcp"]["auth_key"] = "***"
    path = get_config_path()
    typer.echo(f"# Config file: {path}")
    typer.echo(toml.dumps(display_config))


@app.command("set")
def config_set(
    key: str = typer.Argument(help="Config key, e.g. vlm.model"),
    value: str = typer.Argument(help="Config value"),
) -> None:
    """Set a configuration value."""
    set_config_value(key, value)
    typer.echo(f"Set {key} = {value}")


@app.command("doctor")
def config_doctor() -> None:
    """Check whether the vision/grounding model is configured (no network calls)."""
    config = load_config()
    g = config.get("vlm", {})
    api_key = get_api_key()  # reads env var OR config (DPAPI-decrypted)

    typer.echo("Vision model configuration:")
    typer.echo(f"  enabled:  {g.get('enabled', True)}")
    typer.echo(f"  engine:   {g.get('engine', '(not set)')}")
    typer.echo(f"  model:    {g.get('model', '(not set)')}")
    typer.echo(f"  api_base: {g.get('api_base', '(not set)')}")
    typer.echo(f"  api_key:  {'set' if api_key else 'NOT SET'}")

    ready = g.get("enabled", True) and bool(api_key)
    typer.echo("")
    if ready:
        typer.echo(
            "Status: READY — vision commands (ground/detect/ask-vision) are configured."
        )
    else:
        typer.echo(
            "Status: NOT READY — set vlm.api_key, or see the README."
        )
