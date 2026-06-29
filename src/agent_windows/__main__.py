"""Entry point: dispatch to CLI or Daemon mode based on --daemon flag."""

import sys


def main() -> None:
    if "--daemon" in sys.argv:
        from agent_windows.daemon.server import run_daemon

        run_daemon()
    else:
        # cli.main.main() runs the negative-coordinate argv preprocessing then
        # the Typer app (C1). Direct app() would skip it.
        from agent_windows.cli.main import main as cli_main

        cli_main()


if __name__ == "__main__":
    main()
