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
    import copy
    import toml

    config = load_config()
    display_config = copy.deepcopy(config)
    # 遮蔽 API Key
    if "grounding" in display_config and "api_key" in display_config["grounding"]:
        key = display_config["grounding"]["api_key"]
        if key and len(key) > 8:
            display_config["grounding"]["api_key"] = key[:4] + "..." + key[-4:]
        elif key:
            display_config["grounding"]["api_key"] = "***"
    path = get_config_path()
    typer.echo(f"# Config file: {path}")
    typer.echo(toml.dumps(display_config))


@app.command("set")
def config_set(
    key: str = typer.Argument(help="Config key, e.g. grounding.model"),
    value: str = typer.Argument(help="Config value"),
) -> None:
    """Set a configuration value."""
    set_config_value(key, value)
    typer.echo(f"Set {key} = {value}")
