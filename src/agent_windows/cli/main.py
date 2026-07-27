"""CLI entry point: Typer app with command registration."""

import re
import sys

import typer

app = typer.Typer(
    name="agent-windows",
    help="Windows Computer-Use CLI for AI Agents.",
    add_completion=False,
    no_args_is_help=True,
)


# ── Negative-coordinate support (C1) ──────────────────────────────────
# Commands whose positional args are integer screen coordinates. Multi-monitor
# desktops put windows at negative coordinates (e.g. x=-1920 on a monitor left
# of the primary). Click/Typer treat a bare ``-1`` token as an unknown option
# and exit 2 before the command runs — there is no Click-native fix on a
# multi-command app (group-level ignore_unknown_options breaks subcommand
# routing). So main() rewrites argv for these commands only: a leading
# ``-<digits>`` token is marked so Click sees a non-option positional, and the
# command's parse_coord strips the marker. The native Rust launcher handles
# the same case natively (commands.rs:376-380); this brings the Python fallback
# (agent-windows-python / --no-daemon) to parity.
_COORD_CMDS = frozenset({
    "click", "scroll", "move", "dblclick", "hover",
    "mouse-down", "mouse-up", "drag", "highlight", "pixel-at",
})

# A leading "-<digits>" (optionally a fractional part) is a coordinate, not a
# flag. No legal option here starts with a digit (they're letters: -s/-o/...).
_NEG_NUMBER_RE = re.compile(r"-\d+(?:\.\d+)?$")

# ── Optional-value flag support (T6) ──────────────────────────────────
# Commands with flags that take an OPTIONAL value (``--flag`` alone = default
# sentinel; ``--flag VALUE`` = the value). Maps command name → the bare long
# flag tokens that need this treatment. Typer's bundled Click fork
# (typer._click) drops the ``flag_value`` parameter on Option, so there is no
# native way to express "optional-value flag" in a Typer command — Click would
# either error "option requires an argument" (bare flag) or swallow the next
# option-like token as the value (``--screenshot --single-pass`` →
# screenshot="--single-pass"). main() works around this by inserting the
# ``__DEFAULT__`` sentinel after a bare flag when the next token is missing or
# option-like; the command body translates ``__DEFAULT__`` back to ``True``.
# The native Rust launcher mirrors this in commands.rs (Task 7).
_DEFAULT_FLAG_VALUE = "__DEFAULT__"
_OPTIONAL_VALUE_FLAGS: dict[str, frozenset[str]] = {
    "ground": frozenset({"--screenshot"}),
}


def _preprocess_argv(argv: list[str]) -> list[str]:
    """Rewrite argv for command-specific edge cases Click/Typer can't express
    natively. Scoped to specific commands so unrelated commands (fs/shell/
    config, etc.) pass through untouched.

    Two passes today:

    1. Negative-coordinate marking (C1): on coordinate commands, a leading
       ``-<digits>`` token is marked so Click sees a non-option positional.
    2. Optional-value flag sentinel (T6): on commands in
       ``_OPTIONAL_VALUE_FLAGS``, a bare ``--flag`` followed by an option-like
       token (or end of argv) is given the ``__DEFAULT__`` sentinel so the
       command body can map it back to ``True``.

    Stops at a literal ``--`` (Click's raw-positional sentinel) in both passes
    to preserve its native semantics. Returns a new list; never mutates the
    caller's argv.
    """
    if len(argv) < 2:
        return argv
    cmd = argv[1]
    out = list(argv)

    # Pass 1: negative-coordinate marking.
    if cmd in _COORD_CMDS:
        for i in range(2, len(out)):
            if out[i] == "--":
                break  # everything after `--` is already a positional to Click
            if _NEG_NUMBER_RE.fullmatch(out[i]):
                from agent_windows.cli.commands.input import _NEG_COORD_MARKER
                out[i] = _NEG_COORD_MARKER + out[i]

    # Pass 2: optional-value flag sentinel. Walk with a while-loop because we
    # may insert tokens. A single ``-`` is stdin, not an option, so only treat
    # tokens starting with ``-`` and length > 1 as option-like (matches Click's
    # _flag_needs_value check in _click/parser.py).
    optional_flags = _OPTIONAL_VALUE_FLAGS.get(cmd)
    if optional_flags:
        i = 2
        while i < len(out):
            tok = out[i]
            if tok == "--":
                break
            if tok in optional_flags:
                next_tok = out[i + 1] if i + 1 < len(out) else None
                looks_option_like = (
                    next_tok is not None
                    and len(next_tok) > 1
                    and next_tok.startswith("-")
                )
                if next_tok is None or looks_option_like:
                    out.insert(i + 1, _DEFAULT_FLAG_VALUE)
                    i += 2  # skip the sentinel we just inserted
                    continue
            i += 1

    return out


def main() -> None:
    """Console-script entry point: rewrite argv for negative coords, then run."""
    sys.argv = _preprocess_argv(sys.argv)
    app()


# ── Global options ────────────────────────────────────────────────


@app.callback(invoke_without_command=True)
def global_options(
    ctx: typer.Context,
    no_daemon: bool = typer.Option(False, "--no-daemon", help="Skip daemon, execute directly (debug)"),
    format: str = typer.Option("text", "--format", help="Output format: text|json"),
    debug: bool = typer.Option(False, "--debug", help="Enable verbose logging"),
    config: str = typer.Option(
        None, "--config",
        help="Path to an alternate config.toml (default: ~/.agent-windows/config.toml). "
             "Read by this process and propagated to the daemon. State files "
             "(pid/lock/version/logs/screenshots) stay in the default location."),
) -> None:
    """agent-windows — Windows Computer-Use CLI for AI Agents."""
    if config:
        from agent_windows.utils.config import set_config_override
        set_config_override(config)
    # Debug mode (TODO #21-3): set the env var once at the entry point. The
    # spawned daemon inherits it automatically (no --debug threading through
    # the IPC layers), and any module reads it via is_debug().
    if debug:
        from agent_windows.utils.debug import enable_debug
        enable_debug()
    ctx.ensure_object(dict)
    ctx.obj["no_daemon"] = no_daemon
    ctx.obj["format"] = format
    ctx.obj["debug"] = debug
    ctx.obj["config"] = config


# ── Register top-level commands ───────────────────────────────────
# Commands defined in individual modules, added here to the main app.

from agent_windows.cli.commands.observe import observe, snapshot, screenshot, ground, ask_vision, detect, cleanup, highlight, diff_screenshot, snapshot_diff, get_text, is_cmd, find_image, pixel_at, list_displays  # noqa: E402
from agent_windows.cli.commands.input import click, type_text, scroll, move, dblclick, hover, mouse_down, mouse_up, drag, shortcut, key_down, key_up, expand, collapse  # noqa: E402
from agent_windows.cli.commands.system import (  # noqa: E402
    shell, batch, notify,
    app_cmd, fs_cmd, clip_cmd, proc_cmd, registry_cmd,
    lock, sleep, shutdown, restart,
)
from agent_windows.cli.commands.wait import wait, wait_for  # noqa: E402
from agent_windows.cli.commands.multi import multi_select, multi_edit  # noqa: E402
from agent_windows.cli.commands.window import window_cmd  # noqa: E402

app.command()(observe)
app.command()(snapshot)
app.command(name="list-displays")(list_displays)
app.command()(screenshot)
app.command()(ground)
app.command()(ask_vision)
app.command()(detect)
app.command(name="find-image")(find_image)
app.command(name="pixel-at")(pixel_at)
app.command()(cleanup)
app.command()(highlight)
app.command(name="diff")(diff_screenshot)
app.command(name="snapshot-diff")(snapshot_diff)
app.command()(get_text)
app.command()(click)
app.command(name="type")(type_text)
app.command()(scroll)
app.command()(move)
app.command()(dblclick)
app.command()(hover)
app.command()(mouse_down)
app.command()(mouse_up)
app.command()(drag)
app.command()(shortcut)
app.command()(key_down)
app.command()(key_up)
app.command()(expand)
app.command()(collapse)
app.command()(shell)
app.command()(batch)
app.command()(notify)
app.command()(lock)
app.command()(sleep)
app.command()(shutdown)
app.command()(restart)
app.command(name="multi-select")(multi_select)
app.command(name="multi-edit")(multi_edit)
app.command()(wait)
app.command(name="wait-for")(wait_for)

# ── Register sub-command groups ───────────────────────────────────

app.add_typer(app_cmd, name="app")
app.add_typer(fs_cmd, name="fs")
app.add_typer(clip_cmd, name="clipboard")
app.add_typer(proc_cmd, name="process")
app.add_typer(is_cmd, name="is")
app.add_typer(registry_cmd, name="registry")
app.add_typer(window_cmd, name="window")

# ── Daemon and Config groups ──────────────────────────────────────

from agent_windows.cli.commands.daemon_cmd import app as daemon_app, ping  # noqa: E402
from agent_windows.cli.commands.config_cmd import app as config_app  # noqa: E402
from agent_windows.cli.commands.mcp_cmd import app as mcp_app  # noqa: E402
from agent_windows.cli.commands.audit import app as audit_app  # noqa: E402

# `ping` is a top-level command (README/launcher expose it there), defined in
# daemon_cmd.py alongside start/stop/status but registered at the root rather
# than under the `daemon` group (C2).
app.command()(ping)

app.add_typer(daemon_app, name="daemon")
app.add_typer(config_app, name="config")
app.add_typer(mcp_app, name="mcp")
app.add_typer(audit_app, name="audit")
