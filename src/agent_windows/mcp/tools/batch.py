"""Batch + multi-interaction MCP tools.

batch runs several daemon methods in one IPC round-trip (sequential, single STA
worker). multi_select / multi_edit click/type several targets in one call.
"""
from mcp.types import ToolAnnotations

from agent_windows.mcp._call import _call, _as_bool

# Daemon method name → MCP tool name for the cases where they differ. The MCP
# tool filter (tools/exclude_tools) operates on MCP tool names, while ``batch``
# sub-commands name daemon methods. These three are the only mismatches (MCP
# uses underscores where the daemon uses a hyphen, and the wait tools expose a
# single poll probe). Mapping lets the batch filter honor the MCP tool name
# space precisely — otherwise excluding e.g. ``ask_vision`` wouldn't block a
# ``batch({"method": "ask-vision"})`` call. (review #7)
_METHOD_TO_TOOL = {
    "ask-vision": "ask_vision",
    "wait_check": "wait",
    "wait_for_check": "wait_for",
}


def batch_fn(commands: list[dict], stop_on_error: bool = True, delay: float = 0.0) -> str:
    if not isinstance(commands, list) or not commands:
        raise ValueError("commands must be a non-empty list of {method, params}")

    # Enforce the MCP tool filter on sub-commands (review #7). The filter trims
    # *discovery* (which tools a client sees); without this, ``batch`` would
    # forward any excluded method straight to the daemon, bypassing the filter.
    # Gate on whether a server was built (a filter is active), NOT on whether the
    # allowed set is non-empty: an over-restrictive filter (--tools typo) empties
    # the registry, and that must fail CLOSED (block all via batch), not silently
    # disable enforcement. When no server is built (unit tests, library use) the
    # gate is False → allow all, preserving the default.
    #
    # A method resolves to its MCP tool name (via _METHOD_TO_TOOL, else identity)
    # and must be in the surviving registry. Methods with no MCP tool at all
    # (e.g. the internal 'shutdown' handler) resolve by identity and are thus
    # blocked whenever a filter is active — a deliberate side effect: a daemon
    # method not exposed as an MCP tool shouldn't be reachable through batch.
    # If ANY sub-command is blocked, the whole batch aborts (fail-fast) with the
    # offenders named, so the caller can drop them and retry.
    from agent_windows.mcp.server import mcp_server_built, registered_tool_names
    if mcp_server_built():
        allowed_tools = registered_tool_names()
        blocked = [
            c.get("method", "") for c in commands
            if _METHOD_TO_TOOL.get(c.get("method", ""), c.get("method", ""))
            not in allowed_tools
        ]
        if blocked:
            raise ValueError(
                "batch sub-command(s) blocked by the MCP tool filter: "
                + ", ".join(sorted(set(blocked)))
                + ". These methods are excluded via tools/exclude_tools and "
                "cannot be invoked through batch."
            )

    return str(_call("batch", commands=commands, stop_on_error=stop_on_error,
                     delay=delay).get("output", ""))


def multi_select_fn(
    locs: list[list[int]] | None = None,
    selectors: list[str] | None = None,
    press_ctrl: bool | str = True,
) -> str:
    if locs is None and selectors is None:
        raise ValueError("provide either locs or selectors")
    if locs is not None and selectors is not None:
        raise ValueError("provide locs OR selectors, not both")
    return str(_call("multi_select", locs=locs, selectors=selectors,
                     press_ctrl=_as_bool(press_ctrl)).get("output", ""))


def multi_edit_fn(
    locs: list[list] | None = None,
    selectors: list[list] | None = None,
) -> str:
    if locs is None and selectors is None:
        raise ValueError("provide either locs or selectors")
    if locs is not None and selectors is not None:
        raise ValueError("provide locs OR selectors, not both")
    return str(_call("multi_edit", locs=locs, selectors=selectors).get("output", ""))


def register(mcp) -> None:
    _REG = ToolAnnotations

    @mcp.tool(name="batch", description=(
        "Execute multiple daemon commands in one IPC round-trip. "
        "Keywords: batch, multiple commands, sequence, script, run several. "
        "commands is a list of {\"method\": str, \"params\": dict}. "
        "stop_on_error (default true) halts on the first error; delay is "
        "seconds between commands. Nested batch is not allowed. When a "
        "tools/exclude_tools filter is active, any sub-command whose method is "
        "filtered out aborts the whole batch with the offenders named (drop "
        "them and retry)."
    ), annotations=_REG(title="Batch", readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False))
    def _batch(commands: list[dict], stop_on_error: bool = True, delay: float = 0.0) -> str:
        return batch_fn(commands=commands, stop_on_error=stop_on_error, delay=delay)

    @mcp.tool(name="multi_select", description=(
        "Click multiple targets in one call. Keywords: multi select, select "
        "multiple, several clicks, ctrl-click, multi-select items. Provide "
        "locs=[[x,y],...] OR selectors=[\"@e1\",...]. press_ctrl (default true) "
        "holds Ctrl so each click toggles selection."
    ), annotations=_REG(title="Multi Select", readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False))
    def _multi_select(locs: list[list[int]] | None = None,
                      selectors: list[str] | None = None,
                      press_ctrl: bool | str = True) -> str:
        return multi_select_fn(locs=locs, selectors=selectors, press_ctrl=press_ctrl)

    @mcp.tool(name="multi_edit", description=(
        "Type into multiple fields in one call. Keywords: multi edit, fill form, "
        "type multiple, batch type, fill several fields. Provide "
        "locs=[[x,y,text],...] OR selectors=[[@eN,text],...]. Each field is "
        "clicked, cleared, typed."
    ), annotations=_REG(title="Multi Edit", readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False))
    def _multi_edit(locs: list[list] | None = None,
                    selectors: list[list] | None = None) -> str:
        return multi_edit_fn(locs=locs, selectors=selectors)
