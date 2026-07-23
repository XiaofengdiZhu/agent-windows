"""Tests for the `window` CLI command group (P4.2).

Verifies the group exposes all five subcommands and each forwards the target
to the dispatcher with the right daemon method name. _cmd is patched so no
daemon/IPC call happens.
"""

from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from agent_windows.cli.main import app

runner = CliRunner()


def test_window_group_lists_five_subcommands():
    r = runner.invoke(app, ["window", "--help"])
    assert r.exit_code == 0
    for sub in ("minimize", "maximize", "restore", "close", "focus"):
        assert sub in r.output


@pytest.mark.parametrize("sub,method", [
    ("minimize", "window_minimize"),
    ("maximize", "window_maximize"),
    ("restore", "window_restore"),
    ("close", "window_close"),
    ("focus", "window_focus"),
])
def test_subcommand_forwards_target(sub, method):
    with patch("agent_windows.cli.commands.window._cmd") as m:
        r = runner.invoke(app, ["window", sub, "Notepad"])
    assert r.exit_code == 0
    m.assert_called_once()
    assert m.call_args.args[1] == method
    assert m.call_args.kwargs == {"target": "Notepad"}


def test_no_target_defaults_to_foreground():
    """`window minimize` with no target → target='' (foreground window)."""
    with patch("agent_windows.cli.commands.window._cmd") as m:
        r = runner.invoke(app, ["window", "minimize"])
    assert r.exit_code == 0
    assert m.call_args.kwargs == {"target": ""}
