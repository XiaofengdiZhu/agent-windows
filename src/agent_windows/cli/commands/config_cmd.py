"""Config commands: init, show, set."""

import typer

from agent_windows.utils.config import (
    init_config,
    load_config,
    set_config_value,
    get_config_path,
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
    import toml

    config = load_config()
    path = get_config_path()
    typer.echo(f"# Config file: {path}")
    typer.echo(toml.dumps(config))


@app.command("set")
def config_set(
    key: str = typer.Argument(help="Config key, e.g. grounding.model"),
    value: str = typer.Argument(help="Config value"),
) -> None:
    """Set a configuration value."""
    set_config_value(key, value)
    typer.echo(f"Set {key} = {value}")
