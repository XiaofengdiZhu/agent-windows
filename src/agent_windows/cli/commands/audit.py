"""Audit commands: verify the destructive-op audit log integrity."""

import typer

app = typer.Typer(help="Audit-log management")


@app.command("verify")
def verify() -> None:
    """Verify the integrity of the destructive-op audit log.

    Recomputes every line's HMAC and reports verified / mismatched / malformed
    counts. Exit 0 if every line verifies (or the log is absent/empty); exit 1
    if any line is mismatched or malformed. Operator action — there is no MCP
    tool variant, so ``manifest.json`` is unchanged. (audit)
    """
    from agent_windows.core.audit import verify_log
    from agent_windows.utils.config import get_log_dir

    report = verify_log()
    path = get_log_dir() / "action_audit.log"
    if report.total == 0 and not report.malformed:
        typer.echo(f"No audit log at {path}")
        return
    typer.echo(f"# Audit log: {path}")
    typer.echo(f"lines: {report.total}  verified: {report.verified}")
    if report.mismatched:
        typer.echo(f"MISMATCHED ({len(report.mismatched)}): {report.mismatched}")
    if report.malformed:
        typer.echo(f"MALFORMED ({len(report.malformed)}): {report.malformed}")
    if not report.ok:
        raise typer.Exit(code=1)
