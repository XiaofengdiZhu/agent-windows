"""Batch handler: execute multiple commands in a single IPC request."""

import time
from typing import Any

from agent_windows.daemon.router import handler
from agent_windows.ipc.protocol import JSONRPCRequest

MAX_BATCH_SIZE = 50


@handler("batch")
def _batch(params: dict) -> dict[str, Any]:
    """Execute multiple commands sequentially in a single IPC request.

    Params:
        commands: list of {"method": str, "params": dict (optional)}
        stop_on_error: bool (default True) — stop on first error
        delay: float (default 0.0) — seconds between commands

    Returns:
        {"output": ..., "results": [...], "total": int, "success": int}
    """
    # Import here to avoid circular dependency at module level
    from agent_windows.daemon.router import route_request

    commands = params.get("commands", [])
    if not isinstance(commands, list) or not commands:
        raise ValueError("commands must be a non-empty list")

    if len(commands) > MAX_BATCH_SIZE:
        raise ValueError(f"Too many commands in batch (max {MAX_BATCH_SIZE}, got {len(commands)})")

    stop_on_error = params.get("stop_on_error", True)
    delay = params.get("delay", 0.0)
    if not isinstance(delay, (int, float)) or delay < 0:
        raise ValueError("delay must be a non-negative number")

    results: list[dict[str, Any]] = []
    for cmd in commands:
        method = cmd.get("method", "")
        if not method:
            results.append({"method": "", "ok": False, "error": {"code": -32602, "message": "Missing method"}})
            if stop_on_error:
                break
            continue

        if method == "batch":
            results.append({"method": method, "ok": False, "error": {"code": -32602, "message": "Nested batch is not allowed"}})
            if stop_on_error:
                break
            continue

        req = JSONRPCRequest(id=0, method=method, params=cmd.get("params"))
        resp = route_request(req)

        entry: dict[str, Any] = {"method": method, "ok": resp.error is None}
        if resp.error is not None:
            entry["error"] = resp.error
        else:
            entry["result"] = resp.result
        results.append(entry)

        if resp.error is not None and stop_on_error:
            break

        if delay > 0:
            time.sleep(delay)

    ok_count = sum(1 for r in results if r["ok"])
    return {
        "output": f"## Batch\nExecuted: {len(results)}/{len(commands)}\nSuccess: {ok_count}\nFailed: {len(results) - ok_count}",
        "results": results,
        "total": len(results),
        "success": ok_count,
    }
