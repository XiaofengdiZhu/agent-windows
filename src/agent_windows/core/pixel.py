"""Deterministic screen color sampling — read a pixel, optionally match a color.

The color-sensing analogue of :func:`image_match.find_image`: a point query
with a strict per-channel tolerance criterion, no vision-model call, fully
deterministic. Reads the RGB at a screen (or file) coordinate, optionally
samples an ``N×N`` box centered there for robustness against anti-aliasing and
high-DPI jitter, and optionally compares it to a target color within a
per-channel max-diff tolerance.

Coordinate-space parity with ``click``/``find-image``: live source = virtual
screen pixels (via :func:`input_controller._get_virtual_screen`); file source =
image-pixel coords. For a live source, only the clamped sample box is captured
(``capture_screenshot(bbox=…)``), so a single-pixel query is cheap regardless
of screen size.
"""

import logging
from typing import Any

import numpy as np
from PIL import Image

from agent_windows.core.filesystem import validate_read_path, validate_write_path

logger = logging.getLogger(__name__)

# DoS bound on the sample-box side. 101x101 ≈ 10k pixels, instant on any
# backend; caps a runaway --sample value. A larger region would be a region
# statistic (out of scope — that's ``diff``).
MAX_SAMPLE_SIDE = 101


def _parse_color(s: str) -> tuple[int, int, int]:
    """Parse a color string to an ``(r, g, b)`` tuple.

    Accepts (case-insensitive, whitespace-trimmed):

    - ``#RRGGBB``     6 hex digits.
    - ``#RGB``        3 hex digits, each expanded (``#abc`` → aa,bb,cc).
    - ``rgb(r,g,b)``  decimals 0-255, comma- OR space-separated.
    - ``RRGGBB``      bare 6 hex digits (no ``#``).

    Alpha is ignored (screen capture is RGB). Anything else raises
    ``ValueError`` — the caller surfaces this as INVALID_PARAMS.
    """
    if not isinstance(s, str):
        raise ValueError(f"color must be a string, got {type(s).__name__}")
    low = s.strip().lower()

    def _hex6(body: str) -> tuple[int, int, int]:
        if len(body) != 6 or any(c not in "0123456789abcdef" for c in body):
            raise ValueError(f"invalid hex color: {body!r}")
        return (int(body[0:2], 16), int(body[2:4], 16), int(body[4:6], 16))

    def _hex3(body: str) -> tuple[int, int, int]:
        if len(body) != 3 or any(c not in "0123456789abcdef" for c in body):
            raise ValueError(f"invalid hex color: {body!r}")
        r, g, b = (int(c, 16) for c in body)
        return (r * 17, g * 17, b * 17)

    # #RRGGBB or #RGB
    if low.startswith("#"):
        body = low[1:]
        if len(body) == 6:
            return _hex6(body)
        if len(body) == 3:
            return _hex3(body)
        raise ValueError(f"expected #RGB or #RRGGBB, got {s!r}")

    # rgb(r,g,b) — accept commas or whitespace between the three channels.
    if low.startswith("rgb(") and low.endswith(")"):
        inner = low[4:-1]
        parts = inner.split(",") if "," in inner else inner.split()
        if len(parts) != 3:
            raise ValueError(f"rgb() needs 3 channels, got {s!r}")
        try:
            vals = [int(p.strip()) for p in parts]
        except ValueError:
            raise ValueError(f"invalid rgb() color: {s!r}")
        for v in vals:
            if not 0 <= v <= 255:
                raise ValueError(f"rgb() channel out of range 0-255: {s!r}")
        return (vals[0], vals[1], vals[2])

    # Bare RRGGBB (6 hex digits, no #).
    if len(low) == 6:
        return _hex6(low)

    raise ValueError(f"unparseable color: {s!r}")


def _sample_box(
    cx: int, cy: int, sample: int, bounds: tuple[int, int, int, int],
) -> tuple[int, int, int, int]:
    """Return the clamped sample box ``(left, top, right, bottom)``.

    Right/bottom are exclusive, suitable for ``PIL.Image.crop`` /
    ``numpy`` slicing and as a ``capture_screenshot(bbox=…)`` rect. ``bounds``
    is ``(left, top, width, height)`` of the source rect — the virtual screen
    for live capture or ``(0, 0, W, H)`` for a file.

    The box is clamped to the source rect so a corner sample (e.g. a taskbar
    LED) averages the surviving pixels rather than raising. The caller must
    have already validated that the CENTER ``(cx, cy)`` is strictly inside
    ``bounds``.
    """
    bl, bt, bw, bh = bounds
    half = (sample - 1) // 2
    l = cx - half
    t = cy - half
    r = l + sample
    b = t + sample
    # Clamp to source rect; after clamping, (r-l) * (b-t) is the actual pixel
    # count feeding the average.
    l = max(bl, l)
    t = max(bt, t)
    r = min(bl + bw, r)
    b = min(bt + bh, b)
    return (l, t, r, b)


def _luminance(rgb: tuple[int, int, int]) -> int:
    """Convert RGB to a single luma value using PIL's ``L``-mode formula.

    Routing both the sampled region (via ``Image.convert("L")``) and the
    target color through PIL guarantees identical math on both sides of the
    comparison, so a tolerance=0 grayscale match is exact when the luma
    values agree.
    """
    return int(Image.new("RGB", (1, 1), tuple(rgb)).convert("L").getpixel((0, 0)))


def _draw_label(draw, label: str, x: int, y: int, font) -> None:
    """Draw ``label`` at (x, y) on a solid white badge — house style mirrored
    from :func:`image_match._draw_label` / :func:`screenshot._draw_label`.

    A white badge with black text reads cleanly on any underlying pixel color,
    including the crosshair-red and swatch fills pixel-at draws.
    """
    text_pos = (x + 1, y + 1)
    text_bbox = draw.textbbox(text_pos, label, font=font)
    text_bg = [
        text_bbox[0] - 1, text_bbox[1] - 1,
        text_bbox[2] + 1, text_bbox[3] + 1,
    ]
    draw.rectangle(text_bg, fill="white")
    draw.text(text_pos, label, fill="black", font=font)


def _save_pixel_screenshot(
    image: Image.Image,
    cx_in_image: int,
    cy_in_image: int,
    box_in_image: tuple[int, int, int, int],
    hex_label: str,
    output: str,
) -> str:
    """Draw crosshair + sample-box outline + swatch/label, save, return path.

    The caller chooses what to pass as ``image``:

    - File source: a copy of the FULL source image, with ``cx_in_image`` and
      ``box_in_image`` in source-pixel coords (so the annotation shows the
      pixel in its surrounding context).
    - Live source: the captured region (already just the clamped sample box),
      with ``cx``/``box`` translated to region-local coords — there is no
      surrounding context to show because we deliberately captured only the
      box.
    """
    from PIL import ImageDraw, ImageFont

    from agent_windows.core.screenshot import save_and_cleanup

    marked = image.copy().convert("RGB")
    draw = ImageDraw.Draw(marked)
    try:
        font = ImageFont.load_default(size=14)
    except TypeError:
        font = ImageFont.load_default()

    # Sample-box outline (skip if it degenerated to nothing under clamping).
    bl, bt, br, bb = box_in_image
    if br > bl and bb > bt:
        draw.rectangle([(bl, bt), (br - 1, bb - 1)], outline="red", width=2)

    # Crosshair at the queried center.
    arm = 8
    draw.line(
        [(cx_in_image - arm, cy_in_image), (cx_in_image + arm, cy_in_image)],
        fill="red", width=1,
    )
    draw.line(
        [(cx_in_image, cy_in_image - arm), (cx_in_image, cy_in_image + arm)],
        fill="red", width=1,
    )

    # Color swatch + hex label, anchored just above the sample box's top-left.
    swatch_w, swatch_h = 28, 16
    sx = bl
    sy = max(0, bt - swatch_h - 2)
    draw.rectangle([(sx, sy), (sx + swatch_w, sy + swatch_h)], fill=hex_label)
    _draw_label(draw, hex_label, sx + swatch_w + 2, sy, font)

    return str(save_and_cleanup(marked, output_path=output))


def pixel_at(
    x: int,
    y: int,
    source: str | None = None,
    sample: int = 1,
    match: str | None = None,
    tolerance: int = 0,
    grayscale: bool = False,
    output: str | None = None,
) -> dict[str, Any]:
    """Read the color at ``(x, y)`` and optionally compare to a target color.

    Args:
        x, y: The queried point. Integers. Live source = virtual-screen pixel
            coords (same space as ``click``); file source = image-pixel coords.
            Non-integral floats are rejected (no silent floor — find-image
            M-1 lesson).
        source: Path to a source image file. None = live screen capture.
        sample: Sample an N×N box centered on (x, y) and average. Default 1.
            Capped at :data:`MAX_SAMPLE_SIDE`. The box is clamped to source
            bounds at edges (no error); the average divides by the surviving
            pixel count.
        match: Optional target color string (see :func:`_parse_color`). When
            given, the result includes a ``match`` block.
        tolerance: Per-channel max abs diff 0-255 (0 = exact). Default 0.
        grayscale: Convert region + target to luma before sampling/matching.
            The reported ``rgb`` is the luma replicated across all three
            channels (so ``hex`` is a gray).
        output: Optional path. When given, save an annotated capture
            (crosshair + sample box + swatch + hex label) and set
            ``screenshot_path``. pixel-at does NOT auto-save — only on request.

    Returns:
        Dict with ``x``, ``y``, ``rgb`` (3-int list), ``hex`` (lowercase
        ``#rrggbb``), ``sample``, ``method`` == "pixel", and
        ``screenshot_path`` (``""`` unless ``output`` given). When ``match``
        is given, also a ``match`` dict: ``{target, tolerance, is_match,
        max_diff, score}``.

    Raises:
        ValueError: bad coords (out of bounds, non-int float, bool), bad
            sample side, bad tolerance, unparseable color, or a source path
            that does not exist. (These surface as INVALID_PARAMS via
            ``route_request``.)
        PermissionError: blacklisted source/output path (PERMISSION_DENIED).
    """
    # --- coord type: int only. `isinstance(True, int)` is True so reject
    # bools explicitly; reject ALL floats (even integral ones like 1.0) since
    # the contract is `x: int`. (find-image M-1: don't silently int() a
    # non-integral float.)
    if isinstance(x, bool) or not isinstance(x, int):
        raise ValueError(f"x must be an int, got {type(x).__name__}")
    if isinstance(y, bool) or not isinstance(y, int):
        raise ValueError(f"y must be an int, got {type(y).__name__}")

    # --- sample / tolerance range ---
    if isinstance(sample, bool) or not isinstance(sample, int):
        raise ValueError(f"sample must be an int, got {type(sample).__name__}")
    if sample < 1 or sample > MAX_SAMPLE_SIDE:
        raise ValueError(
            f"sample must be in [1, {MAX_SAMPLE_SIDE}], got {sample}")
    if isinstance(tolerance, bool) or not isinstance(tolerance, int):
        raise ValueError(
            f"tolerance must be an int, got {type(tolerance).__name__}")
    if tolerance < 0 or tolerance > 255:
        raise ValueError(f"tolerance must be in [0, 255], got {tolerance}")

    target = _parse_color(match) if match is not None else None

    # --- resolve source: file (blacklist-checked) or live capture. ---
    # bounds is (left, top, width, height) of the source rect in its own
    # coordinate space — virtual-screen for live, image-pixel for file.
    src_pil: Image.Image | None = None
    if source is not None:
        sp = validate_read_path(source)
        if not sp.exists():
            raise ValueError(f"Source image not found: {source}")
        with Image.open(sp) as im:
            im.load()
            src_pil = im.convert("RGB")
        sw, sh = src_pil.size
        bounds = (0, 0, sw, sh)
        # File: strict bounds, no 1px grace (can't sample a non-existent
        # pixel). Plain ValueError → INVALID_PARAMS (not COORDINATE_OUT_OF_RANGE).
        if not (0 <= x < sw and 0 <= y < sh):
            raise ValueError(
                f"({x}, {y}) is outside the source image ({sw}x{sh})")
    else:
        # Live: lazy-import the Win32 + capture bits so the module body stays
        # light and so non-Windows runs that only ever hit the `source=path`
        # branch don't pay the import cost.
        from agent_windows.core.input_controller import _get_virtual_screen
        from agent_windows.core.screenshot import capture_screenshot

        vl, vt, vw, vh = _get_virtual_screen()
        bounds = (vl, vt, vw, vh)
        if not (vl <= x < vl + vw and vt <= y < vt + vh):
            raise ValueError(
                f"({x}, {y}) is outside the virtual screen "
                f"({vl},{vt})-({vl + vw},{vt + vh})")

    box_l, box_t, box_r, box_b = _sample_box(x, y, sample, bounds)

    # --- obtain ONLY the clamped sample box (efficient: one-pixel queries
    # must not capture the full screen). For a file source we also keep the
    # full image for annotation context. ---
    if src_pil is not None:
        region = src_pil.crop((box_l, box_t, box_r, box_b))
        full_for_annotate: Image.Image | None = src_pil
    else:
        region = capture_screenshot(bbox=(box_l, box_t, box_r, box_b))
        full_for_annotate = None

    # --- reduce the region to a single averaged color. ---
    if grayscale:
        arr = np.asarray(region.convert("L"), dtype=np.float64)
        mean_val = float(arr.mean(axis=(0, 1))) if arr.size else 0.0
        luma = int(round(mean_val))
        rgb = (luma, luma, luma)
    else:
        arr = np.asarray(region.convert("RGB"), dtype=np.float64)
        if arr.size:
            means = arr.mean(axis=(0, 1))
            rgb = (
                int(round(means[0])),
                int(round(means[1])),
                int(round(means[2])),
            )
        else:
            rgb = (0, 0, 0)

    hex_str = "#%02x%02x%02x" % rgb

    result: dict[str, Any] = {
        "x": x,
        "y": y,
        "rgb": [rgb[0], rgb[1], rgb[2]],
        "hex": hex_str,
        "sample": sample,
        "method": "pixel",
        "screenshot_path": "",
    }

    # --- match block (only when a target color was given). max_diff is over
    # the 3 channels of the AVERAGED color (or a single luma diff in
    # grayscale mode). score mirrors image_match._score: max-diff, not mean.
    if target is not None:
        if grayscale:
            tgt_val = _luminance(target)
            effective_target = (tgt_val, tgt_val, tgt_val)
        else:
            effective_target = target
        diffs = [abs(rgb[i] - effective_target[i]) for i in range(3)]
        max_diff = max(diffs)
        result["match"] = {
            "target": "#%02x%02x%02x" % tuple(target),
            "tolerance": tolerance,
            "is_match": bool(max_diff <= tolerance),
            "max_diff": int(max_diff),
            "score": round(1.0 - max_diff / 255.0, 4),
        }

    # --- optional annotation. ---
    if output is not None:
        op = validate_write_path(output)
        if full_for_annotate is not None:
            # File: annotate a copy of the full source image. The center and
            # sample box are already in source-pixel coords.
            saved = _save_pixel_screenshot(
                full_for_annotate,
                cx_in_image=x,
                cy_in_image=y,
                box_in_image=(box_l, box_t, box_r, box_b),
                hex_label=hex_str,
                output=str(op),
            )
        else:
            # Live: the captured region IS the box. Translate the center and
            # sample box into region-local coords so the crosshair lands on
            # the queried pixel and the outline traces the box edges.
            saved = _save_pixel_screenshot(
                region,
                cx_in_image=x - box_l,
                cy_in_image=y - box_t,
                box_in_image=(0, 0, box_r - box_l, box_b - box_t),
                hex_label=hex_str,
                output=str(op),
            )
        result["screenshot_path"] = saved

    return result
