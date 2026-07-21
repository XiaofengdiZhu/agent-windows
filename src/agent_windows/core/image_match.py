"""Deterministic on-screen template matching (pyautogui ``locateOnScreen`` equiv).

Finds a template image inside a screenshot (or any source image) and returns
click-ready coordinates — no vision-model call, no network, fully deterministic.
Sliding-window pixel match with a strict per-pixel tolerance criterion, optional
grayscale fast-path, and greedy non-max suppression for multi-match.

Result vocabulary mirrors ``ground``/``detect`` so callers can swap it in when a
template image is available instead of a natural-language description.
"""

import logging
import time
from typing import Any

import numpy as np
from PIL import Image

from agent_windows.core.filesystem import validate_read_path
from agent_windows.utils.config import get_screenshot_save_dir

logger = logging.getLogger(__name__)

# DoS bounds. Strict matching is O(positions x template_area x channels); this
# caps template area so a full-1080p match stays well under STA_SUBMIT_TIMEOUT_S.
# 128x128 is ~8s worst case. The sliding-window diff for one scanned source
# row on 1080p holds ~88M elements (~176MB as int16); the astype→subtract→abs
# chain transiently peaks at ~2x that (~350MB). For larger templates, narrow
# the search with `region`. (FFT matching would lift this; deferred.)
MAX_FIND_TEMPLATE_PIXELS = 16_384   # 128x128
MAX_FIND_MATCHES = 100


def _load_template(path: str, grayscale: bool) -> tuple[np.ndarray, int, int]:
    """Load + blacklist-check a template image; return (array, width, height).

    array is (th, tw, 3) for RGB or (th, tw) for grayscale.

    Reads the image header and enforces the pixel cap BEFORE the full decode
    (``img.load()``) so an oversized template is rejected without first being
    materialized into a multi-hundred-MB numpy array (DoS guard). The check
    is repeated in ``find_image`` as a defensive backstop, since this helper
    is independently callable.
    """
    p = validate_read_path(path)
    if not p.exists():
        raise ValueError(f"Template image not found: {path}")
    mode = "L" if grayscale else "RGB"
    with Image.open(p) as img:
        w_i, h_i = img.size                          # header read, no full decode
        if w_i * h_i > MAX_FIND_TEMPLATE_PIXELS:
            raise ValueError(
                f"template too large: {w_i}x{h_i}={w_i * h_i} px exceeds the "
                f"{MAX_FIND_TEMPLATE_PIXELS}-px maximum (128x128). Use a "
                f"smaller template or narrow the search with region."
            )
        img.load()                                   # full decode into memory
        if img.mode != mode:
            img = img.convert(mode)
        arr = np.asarray(img)
    th, tw = arr.shape[:2]
    return arr, tw, th


def _match_positions(
    src: np.ndarray, tpl: np.ndarray, tolerance: int,
) -> np.ndarray:
    """Return an (N, 3) int array of ``[y, x, worst_diff]`` for every position
    whose maximum per-pixel, per-channel absolute diff is ``<= tolerance``.

    The match criterion is strict: a position qualifies iff EVERY pixel and
    channel is within ``tolerance``; ``worst_diff`` is that position's largest
    single pixel/channel difference (0 for an exact match). ``worst_diff`` is
    also the NMS/ordering key (tightest match first).

    Iterates one source row of output positions at a time so peak memory is one
    scan row (bounded by the template-size cap), not the full 5D diff tensor.
    """
    from numpy.lib.stride_tricks import sliding_window_view

    h, w = src.shape[:2]
    th, tw = tpl.shape[:2]
    if th > h or tw > w:
        raise ValueError("template is larger than the source image")

    rgb = src.ndim == 3                       # else grayscale 2D
    reduce_axes = (0, 2, 3) if rgb else (0, 2)
    out_y = h - th + 1
    rows: list[np.ndarray] = []
    tpl_i = tpl[:, None, ...].astype(np.int16)   # (th, 1, tw[, C]) for broadcast
    for y in range(out_y):
        src_row = src[y : y + th]                       # (th, W[, C])
        windows = sliding_window_view(src_row, tw, axis=1)
        # sliding_window_view appends the window axis at the end; for RGB that
        # yields (th, out_x, C, tw). Move it before C so the layout matches
        # tpl_i (th, 1, tw[, C]). Grayscale is already (th, out_x, tw).
        if rgb:
            windows = np.moveaxis(windows, -1, -2)
        diff = np.abs(windows.astype(np.int16) - tpl_i)
        row_max = diff.max(axis=reduce_axes)            # (out_x,) worst diff per x
        xs = np.nonzero(row_max <= tolerance)[0]
        if xs.size:
            ys = np.full(xs.shape, y, dtype=np.int64)
            rows.append(np.stack([ys, xs, row_max[xs].astype(np.int64)], axis=1))
    if not rows:
        return np.empty((0, 3), dtype=np.int64)
    return np.concatenate(rows, axis=0)


def _nms(
    positions: np.ndarray, tw: int, th: int, cap: int,
) -> tuple[list[int], bool]:
    """Greedy non-max suppression over positions already sorted best-first.

    Suppress any position whose (x, y) is within one template footprint (tw, th)
    of an already-kept one — collapses the overlapping cluster a single target
    produces into one box. Returns ``(kept_indices, truncated)`` where
    ``kept_indices`` has length <= cap and ``truncated`` is True iff the cap
    was hit (so callers can surface a truncation flag, matching the
    ``core/filesystem.py`` convention for capped ops).
    """
    kept: list[int] = []
    truncated = False
    for i in range(positions.shape[0]):
        if len(kept) >= cap:
            truncated = True
            break
        yi, xi = positions[i, 0], positions[i, 1]
        if any(abs(yi - positions[k, 0]) < th and abs(xi - positions[k, 1]) < tw
               for k in kept):
            continue
        kept.append(i)
    return kept, truncated


def _score(src: np.ndarray, tpl: np.ndarray, y: int, x: int) -> float:
    """Match score in [0, 1] = 1 - max_abs_diff/255 over the template footprint
    at (y, x). 1.0 for an exact match. Uses the worst single-pixel/channel
    difference — the same statistic ``_match_positions`` qualifies positions on,
    so the score is the natural analog of the tolerance criterion (a single
    off-by-N pixel caps the score at ``1 - N/255`` rather than being averaged
    away across channels)."""
    window = src[y : y + tpl.shape[0], x : x + tpl.shape[1]]
    worst_diff = int(np.abs(window.astype(np.int16) - tpl.astype(np.int16)).max())
    return round(1.0 - worst_diff / 255.0, 4)


def find_image(
    template: str,
    source: str | None = None,
    region: tuple[int, int, int, int] | None = None,
    tolerance: int = 0,
    grayscale: bool = False,
    all_matches: bool = False,
) -> dict[str, Any]:
    """Locate ``template`` on screen (or in ``source``) and return coordinates.

    Args:
        template: Path to the template image file to locate.
        source: Path to a source screenshot. None = capture a fresh screenshot
            (at native resolution — exact match needs unscaled pixels).
        region: Optional (x1, y1, x2, y2) search ROI in source coords. Offsets
            are added back to results so returned coords are in source space.
        tolerance: 0-255 per-channel max abs diff. 0 = exact pixel match.
        grayscale: Convert both to grayscale before matching (~3x faster, less
            discriminative on color).
        all_matches: False (default) = return only the best match (plus a flat
            ``x, y`` center for ground-parity). True = return every NMS-deduped
            match above tolerance.

    Returns:
        Dict with ``count``, ``matches`` (each {index, x, y, w, h, center_x,
        center_y, score}), ``method`` == "template", ``truncated`` (bool — True
        iff the ``MAX_FIND_MATCHES`` cap was hit in multi-match mode), and
        ``screenshot_path``. Single-match mode also flattens the best match's
        center to top-level ``x, y``. Raises ValueError on bad paths, oversized
        templates, out-of-bounds region, or template larger than source.
    """
    tpl, tw, th = _load_template(template, grayscale)
    # Defensive backstop: _load_template already enforces this from the image
    # header before the full decode, but _load_template is independently
    # callable so the bound is re-checked here on the materialized array.
    # (`--bbox` is a CLI/handler concern; the Python parameter is `region`.)
    if tw * th > MAX_FIND_TEMPLATE_PIXELS:
        raise ValueError(
            f"template too large: {tw}x{th}={tw * th} px exceeds the "
            f"{MAX_FIND_TEMPLATE_PIXELS}-px maximum (128x128). Use a smaller "
            f"template or narrow the search with region."
        )

    # Source: file (blacklist-checked) or fresh capture. Keep a full RGB PIL copy
    # for annotation; derive the matching array (RGB or L) from it.
    if source:
        sp = validate_read_path(source)
        if not sp.exists():
            raise ValueError(f"Source image not found: {source}")
        with Image.open(sp) as im:
            im.load()
            src_pil = im.convert("RGB")
    else:
        from agent_windows.core.screenshot import capture_screenshot
        src_pil = capture_screenshot().convert("RGB")

    sw, sh = src_pil.size
    mode = "L" if grayscale else "RGB"
    src = np.asarray(src_pil.convert(mode))

    # Apply search region to the numpy array; track origin offset for results.
    off_x = off_y = 0
    if region:
        x1, y1, x2, y2 = region
        if x2 <= x1 or y2 <= y1:
            raise ValueError("region must have x2 > x1 and y2 > y1")
        if x1 < 0 or y1 < 0 or x2 > sw or y2 > sh:
            raise ValueError(
                f"region ({x1},{y1},{x2},{y2}) is outside the source ({sw}x{sh})")
        src = src[y1:y2, x1:x2]
        off_x, off_y = x1, y1

    positions = _match_positions(src, tpl, tolerance)     # (N,3) [y,x,worst]
    if positions.shape[0] == 0:
        logger.info("find-image: no match (tolerance=%d, grayscale=%s)", tolerance, grayscale)
        return {
            "count": 0,
            "matches": [],
            "method": "template",
            "truncated": False,
            "screenshot_path": "",
        }

    # Tightest match first (lowest worst_diff), then NMS or best-only.
    # kind="stable" makes the kept (x,y) deterministic across runs for a
    # uniform cluster of equally-good positions (score is unaffected).
    order = np.argsort(positions[:, 2], kind="stable")
    positions = positions[order]
    if all_matches:
        kept, truncated = _nms(positions, tw, th, MAX_FIND_MATCHES)
    else:
        kept, truncated = [0], False

    matches: list[dict[str, Any]] = []
    for idx, pos_i in enumerate(kept, 1):
        y, x = int(positions[pos_i, 0]), int(positions[pos_i, 1])
        sx, sy = x + off_x, y + off_y
        matches.append({
            "index": idx,
            "x": sx, "y": sy,
            "w": tw, "h": th,
            "center_x": sx + tw // 2,
            "center_y": sy + th // 2,
            "score": _score(src, tpl, y, x),
        })

    if truncated:
        logger.info(
            "find-image: hit %d-match cap, results truncated", MAX_FIND_MATCHES,
        )

    result: dict[str, Any] = {
        "count": len(matches),
        "matches": matches,
        "method": "template",
        "truncated": truncated,
        "screenshot_path": _save_match_screenshot(src_pil, matches),
    }
    if not all_matches and matches:
        result["x"] = matches[0]["center_x"]
        result["y"] = matches[0]["center_y"]
    logger.info("find-image: %d match(es) for %s", len(matches), template)
    return result


def _save_match_screenshot(src_pil: Image.Image, matches: list[dict[str, Any]]) -> str:
    """Draw red rectangles around each match on a copy of the source, save it,
    return the path. Match coords are already in full-source space (region
    offsets were applied), so they map directly onto ``src_pil``."""
    from PIL import ImageDraw, ImageFont

    from agent_windows.core.screenshot import save_and_cleanup

    if not matches:
        return ""
    marked = src_pil.copy()
    draw = ImageDraw.Draw(marked)
    try:
        font = ImageFont.load_default(size=14)
    except TypeError:
        font = ImageFont.load_default()
    for m in matches:
        x, y, w, h = m["x"], m["y"], m["w"], m["h"]
        draw.rectangle([(x, y), (x + w - 1, y + h - 1)], outline="red", width=2)
        _draw_label(draw, f"#{m['index']}", x, y, font)

    save_dir = get_screenshot_save_dir()
    path = save_dir / f"find-image-{int(time.time() * 1000)}.png"
    return str(save_and_cleanup(marked, output_path=str(path)))


def _draw_label(draw, label: str, x: int, y: int, font) -> None:
    """Draw a label badge at (x, y) with a solid background for legibility.

    Mirrors ``screenshot.py``'s ``_draw_label``: a white badge with black text
    reads cleanly on a red match rectangle, where bare red-on-red text would
    vanish. (House style.)
    """
    text_pos = (x + 3, y + 3)
    text_bbox = draw.textbbox(text_pos, label, font=font)
    text_bg = [
        text_bbox[0] - 1, text_bbox[1] - 1,
        text_bbox[2] + 1, text_bbox[3] + 1,
    ]
    draw.rectangle(text_bg, fill="white")
    draw.text(text_pos, label, fill="black", font=font)
