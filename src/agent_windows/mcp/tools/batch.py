"""Batch + multi-interaction MCP tools.

batch runs several daemon methods in one IPC round-trip (sequential, single STA
worker). multi_select / multi_edit click/type several targets in one call.
"""
from mcp.types import ToolAnnotations

from agent_windows.mcp._call import _call, _as_bool


def batch_fn(commands: list[dict], stop_on_error: bool = True, delay: float = 0.0) -> str:
    if not isinstance(commands, list) or not commands:
        raise ValueError("commands must be a non-empty list of {method, params}")
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
        "seconds between commands. Nested batch is not allowed."
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
