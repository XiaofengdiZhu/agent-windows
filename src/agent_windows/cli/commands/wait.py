"""Wait command."""

import time

import typer


def wait(
    seconds: float = typer.Argument(help="Seconds to wait"),
) -> None:
    """Wait for the specified number of seconds."""
    time.sleep(seconds)
