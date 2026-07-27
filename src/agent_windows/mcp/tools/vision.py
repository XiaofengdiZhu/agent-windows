"""Vision MCP tools — ground, detect, ask-vision.

These need a configured multimodal model (vlm.* in config.toml). They offload
image understanding to a dedicated cheap model and return ready-to-use
coordinates/bounding boxes, so the driving agent need not be multimodal.
"""
from typing import Any

from mcp.types import ToolAnnotations

from agent_windows.mcp._call import _call, _as_bool
from agent_windows.mcp.tools.observation import _image_from_result


def ground_fn(
    description: str,
    single_pass: bool | str = False,
    output: str | None = None,
) -> list:
    """Locate an element by natural-language description; returns coords + marked image.

    MCP always marks the screenshot (agent sees the grounding landing point).
    ``output`` overrides the save path (default dir when None). On failure the
    daemon returns no screenshot_path and this falls back to text-only.
    """
    if not description:
        raise ValueError("description is required")
    # Map to the daemon's three-state `screenshot` param: always mark; output
    # → custom path, else True (default dir).
    screenshot: bool | str = True if output is None else output
    result = _call("ground", description=description,
                   single_pass=_as_bool(single_pass) or None,
                   screenshot=screenshot)
    return _image_from_result(result, "screenshot_path")


def detect_fn(
    description: str,
    max_targets: int = 20,
    image: str | None = None,
    model: str | None = None,
    output: str | None = None,
) -> list:
    """Detect multiple targets matching a description; returns bounding boxes + image."""
    if not description:
        raise ValueError("description is required")
    result = _call("detect", description=description, max_targets=max_targets,
                   image=image, model=model, output=output)
    return _image_from_result(result, "screenshot_path")


def ask_vision_fn(
    prompt: str,
    image: str | None = None,
    model: str | None = None,
    output: str | None = None,
) -> str:
    """Free-form visual Q&A: send a screenshot + prompt to the vision model."""
    if not prompt:
        raise ValueError("prompt is required")
    return str(_call("ask-vision", prompt=prompt, image=image,
                     model=model, output=output).get("output", ""))


def register(mcp) -> None:
    @mcp.tool(name="ground", description=(
        "Locate a UI element by natural-language description and return "
        "click-ready coordinates [x, y], confidence, and a marked screenshot. "
        "Keywords: ground, find element, locate, where is, click point, visual "
        "search. Uses the configured vision model (vlm.* in config.toml). "
        "Set single_pass=True for one API call (~2x faster, lower precision). "
        "Precision is ~±100px; prefer snapshot for named controls. "
        "output=<path> saves the marked screenshot there (default dir otherwise)."
    ), annotations=ToolAnnotations(title="Ground", readOnlyHint=True,
                                   idempotentHint=False, openWorldHint=False))
    def _ground(description: str, single_pass: bool | str = False,
                output: str | None = None) -> list:
        return ground_fn(description=description, single_pass=single_pass, output=output)

    @mcp.tool(name="detect", description=(
        "Detect multiple targets matching a description and return a bounding "
        "box per match (plus a marked screenshot). Keywords: detect, find all, "
        "multiple targets, bounding boxes, count occurrences. max_targets caps "
        "the count. Use ground when you need a single click point."
    ), annotations=ToolAnnotations(title="Detect", readOnlyHint=True,
                                   idempotentHint=False, openWorldHint=False))
    def _detect(description: str, max_targets: int = 20,
                image: str | None = None, model: str | None = None,
                output: str | None = None) -> list:
        return detect_fn(description=description, max_targets=max_targets,
                         image=image, model=model, output=output)

    @mcp.tool(name="ask_vision", description=(
        "Free-form visual Q&A: send a screenshot (or a supplied image) plus a "
        "prompt to the vision model and return its textual answer. Keywords: "
        "ask vision, image question, what is this, describe, understand icon, "
        "read chart. Use for understanding icons/states, not for precise "
        "coordinates (use ground)."
    ), annotations=ToolAnnotations(title="Ask Vision", readOnlyHint=True,
                                   idempotentHint=False, openWorldHint=False))
    def _ask_vision(prompt: str, image: str | None = None,
                    model: str | None = None, output: str | None = None) -> str:
        return ask_vision_fn(prompt=prompt, image=image, model=model, output=output)
