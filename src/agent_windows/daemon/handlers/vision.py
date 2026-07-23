"""Vision model handlers: ground, detect, ask-vision."""

from typing import Any

from agent_windows.daemon.router import handler


# DoS bound: ground/detect ``description`` and ask-vision ``prompt`` are spliced
# into the prompt sent to the (blocking) vision model — and ground fires it
# twice (coarse + fine). With no cap, a megabyte input still completes but
# bloats tokens/latency and pins the single STA worker. Reject oversized input
# at the handler edge as INVALID_PARAMS, matching ``type``'s MAX_TYPE_TEXT_LEN.
# (G17)
MAX_DESC_LEN = 2000
MAX_PROMPT_LEN = 8000


@handler("ground")
def _ground(params: dict) -> dict[str, Any]:
    """Visual grounding: locate element coordinates."""
    from agent_windows.core.grounding import ground_element

    description = params.get("description", "")
    if not description:
        raise ValueError("description is required")
    if len(description) > MAX_DESC_LEN:
        raise ValueError(
            f"description length {len(description)} exceeds the "
            f"{MAX_DESC_LEN}-character maximum")

    result = ground_element(description, single_pass=params.get("single_pass"))
    return {
        "output": (
            f"## Grounding Result\n"
            f"Target: \"{description}\"\n"
            f"Coordinate: ({result['x']}, {result['y']})\n"
            f"Confidence: {result['confidence']}\n"
            f"Method: {result['method']}\n"
            f"Screenshot: {result['screenshot_path']} (marked)"
        ),
        "x": result["x"],
        "y": result["y"],
        "confidence": result["confidence"],
        "method": result["method"],
        "screenshot_path": result["screenshot_path"],
    }


@handler("ask-vision")
def _ask_vision(params: dict) -> dict[str, Any]:
    """Free-form visual Q&A: send screenshot + custom prompt to vision model."""
    from agent_windows.core.grounding import ask_vision

    prompt = params.get("prompt", "")
    if not prompt:
        raise ValueError("prompt is required")
    if len(prompt) > MAX_PROMPT_LEN:
        raise ValueError(
            f"prompt length {len(prompt)} exceeds the "
            f"{MAX_PROMPT_LEN}-character maximum")

    result = ask_vision(
        prompt=prompt,
        image_path=params.get("image"),
        model=params.get("model"),
        output_path=params.get("output"),
    )
    return {
        "output": (
            f"## Ask Vision\n"
            f"Prompt: {prompt[:100]}{'...' if len(prompt) > 100 else ''}\n"
            f"Model: {result['model_used']}\n"
            f"Screenshot: {result['screenshot_path']}\n"
            "Note: Use `ground` for precise click coordinates. "
            "This tool is for visual understanding (ranges, states, descriptions).\n\n"
            f"{result['response']}"
        ),
        "response": result["response"],
        "screenshot_path": result["screenshot_path"],
        "model_used": result["model_used"],
    }


@handler("detect")
def _detect(params: dict) -> dict[str, Any]:
    """Visual detection: detect multiple targets with bounding boxes."""
    from agent_windows.core.grounding import detect_elements

    description = params.get("description", "")
    if not description:
        raise ValueError("description is required")
    if len(description) > MAX_DESC_LEN:
        raise ValueError(
            f"description length {len(description)} exceeds the "
            f"{MAX_DESC_LEN}-character maximum")

    max_targets = params.get("max_targets", 20)
    try:
        max_targets = int(max_targets)
    except (ValueError, TypeError):
        max_targets = 20

    result = detect_elements(
        description=description,
        image_path=params.get("image"),
        model=params.get("model"),
        max_targets=max_targets,
        output_path=params.get("output"),
    )

    targets = result["targets"]
    out_targets: list[dict] = []
    if targets:
        lines = [
            "## Detection Result",
            f'Description: "{result["description"]}"',
            f"Count: {result['count']}",
            f"Screenshot: {result['screenshot_path']} (marked)",
            "",
            "Targets:",
        ]
        for t in targets:
            b = t["box"]
            # Click-ready center (bbox midpoint) so the agent needn't compute
            # it — mirrors find_image's center_x/center_y contract. (P3.4)
            cx, cy = (b["x1"] + b["x2"]) // 2, (b["y1"] + b["y2"]) // 2
            out_targets.append({**t, "center_x": cx, "center_y": cy})
            lines.append(
                f"  #{t['index']}: label=\"{t['label']}\", "
                f"box=({b['x1']}, {b['y1']}, {b['x2']}, {b['y2']}), "
                f"center=({cx}, {cy})"
            )
        text_output = "\n".join(lines)
    else:
        text_output = (
            "## Detection Result\n"
            f'Description: "{result["description"]}"\n'
            f"Count: 0\n"
            "No targets found.\n"
            f"Screenshot: {result['screenshot_path']}"
        )

    return {
        "output": text_output,
        "description": result["description"],
        "count": result["count"],
        "targets": out_targets,
        "screenshot_path": result["screenshot_path"],
    }
