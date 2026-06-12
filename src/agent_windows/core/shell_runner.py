"""PowerShell command execution."""

import subprocess
import logging
from typing import Any

logger = logging.getLogger(__name__)


def run_shell(command: str, timeout: float = 30.0) -> dict[str, Any]:
    """Execute a PowerShell command and return the output.

    Args:
        command: PowerShell command string.
        timeout: Execution timeout in seconds.

    Returns:
        Dict with output, exit_code.
    """
    logger.warning("Shell command executed: %s", command[:500])
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
        )

        output_parts = [f"## Shell\nCommand: {command}"]
        if result.stdout.strip():
            output_parts.append(result.stdout.strip())
        if result.stderr.strip():
            output_parts.append(f"[stderr]\n{result.stderr.strip()}")
        output_parts.append(f"Exit code: {result.returncode}")

        return {
            "output": "\n".join(output_parts),
            "exit_code": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"Shell command timed out after {timeout}s: {command}")
    except FileNotFoundError:
        raise RuntimeError("PowerShell not found")
