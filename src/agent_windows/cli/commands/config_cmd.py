"""Config commands: init, show, set."""

import typer

from agent_windows.utils.config import (
    init_config,
    load_config,
    set_config_value,
    get_config_path,
    get_api_key,
    validate_config_file,
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
    # Mask secrets so `config show` never prints them in the clear (they may
    # be captured in screen recordings, shell history, support bundles, …).
    # Masking is done on the decrypted values: a length threshold avoids
    # exposing most of a short secret (a len<=8 secret shows as "***" rather
    # than first-4 + last-4, which for a 9-char token would reveal all but one
    # character). Each key handled here is the same secret-class as the VLM
    # api key.
    def _mask(section: str, key: str) -> None:
        val = display_config.get(section, {}).get(key)
        if val and len(val) > 8:
            display_config[section][key] = val[:4] + "..." + val[-4:]
        elif val:
            display_config[section][key] = "***"

    _mask("vlm", "api_key")
    _mask("mcp", "auth_key")
    _mask("security", "oauth_client_secret")
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
    # Report config type errors first (TODO #10): a mistyped value can make
    # load_config() raise, so check the file directly before the vlm probe.
    errors = validate_config_file()
    if errors:
        typer.echo("Config validation errors:")
        for err in errors:
            typer.echo(f"  {err}")
        typer.echo("")
        typer.echo("Fix the above in config.toml, then re-run `config doctor`.")
        raise typer.Exit(1)

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
