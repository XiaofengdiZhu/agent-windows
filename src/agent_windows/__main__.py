"""Entry point: dispatch to CLI or Daemon mode based on --daemon flag."""

import sys


def main() -> None:
    if "--daemon" in sys.argv:
        from agent_windows.daemon.server import run_daemon

        run_daemon()
    else:
        from agent_windows.cli.main import app

        app()


if __name__ == "__main__":
    main()
