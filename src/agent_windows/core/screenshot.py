"""Screenshot service: multi-backend capture (dxcam → mss → Pillow).

Auto-selects the best available backend. Saves to disk and returns path info.
"""

import atexit
import logging
import threading
import time
from pathlib import Path
from typing import Any

from PIL import Image

from agent_windows.utils.config import load_config, get_screenshot_save_dir

logger = logging.getLogger(__name__)

_dxcam_cameras: dict[int, object] = {}
_dxcam_lock = threading.Lock()

_last_successful_backend: str | None = None

_mss_instance = None
_mss_lock = threading.Lock()

# numpy availability is a process-lifetime constant; probe once at import so
# diff_images doesn't repeat the import attempt on every call. (PERF3-08)
try:
    import numpy as _np  # noqa: F401
    _HAS_NUMPY = True
except ImportError:
    _HAS_NUMPY = False


def release_capture_backends() -> None:
    """Release the cached capture-backend singletons (L-29).

    ``mss`` holds a screen-DC handle and ``dxcam`` holds DXGI resources; both
    were leaked on exit because the module-level singletons were never
    released. Registered with :mod:`atexit` so every exit path (daemon idle
    shutdown, Ctrl+C, the no-daemon CLI path) releases them. Safe to call
    repeatedly — the singletons are lazily re-created on the next capture.

    (Windows reclaims all handles at process death regardless; this makes the
    release explicit and prompt rather than waiting for teardown, and keeps
    long-lived runs from accumulating backend state.)
    """
    global _mss_instance  # rebound below; _dxcam_cameras is mutated in place
    with _mss_lock:
        if _mss_instance is not None:
            try:
                _mss_instance.close()
            except Exception:
                logger.debug("mss.close() failed", exc_info=True)
            _mss_instance = None
    with _dxcam_lock:
        # dxcam's DXCamera exposes no public close(); drop the per-output
        # references so their __del__ releases the DXGI resources via GC.
        # Best-effort.
        _dxcam_cameras.clear()


atexit.register(release_capture_backends)

# ── Virtual screen helpers ────────────────────────────────────────


def get_virtual_screen_rect() -> tuple[int, int, int, int]:
    """Return (left, top, width, height) of the virtual screen (all monitors)."""
    import ctypes

    SM_XVIRTUALSCREEN = 76
    SM_YVIRTUALSCREEN = 77
    SM_CXVIRTUALSCREEN = 78
    SM_CYVIRTUALSCREEN = 79
    user32 = ctypes.windll.user32
    left = user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
    top = user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
    width = user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)
    height = user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)
    return left, top, width, height


def get_monitor_count() -> int:
    """Return number of monitors."""
    import ctypes

    return ctypes.windll.user32.GetSystemMetrics(80)  # SM_CMONITORS


def get_dpi_scale() -> float:
    """Return the system DPI as a multiplier of 96 (1.0 / 1.25 / 1.5 / …).

    Informational only: the daemon is PerMonitorDpiAware, so UIA/Win32
    coordinates are physical pixels and DPI does NOT change click math.
    GetDpiForSystem (Win10 1607+) failing returns 1.0. (TODO #3)
    """
    import ctypes

    try:
        dpi = ctypes.windll.user32.GetDpiForSystem()
        if dpi and dpi > 0:
            return round(dpi / 96.0, 6)
    except Exception:
        logger.debug("GetDpiForSystem failed; DPI scale unknown", exc_info=True)
    return 1.0


def get_capture_backend() -> str | None:
    """Name of the backend used by the most recent capture, or None."""
    return _last_successful_backend


# ── Backend capture functions ─────────────────────────────────────


def _resolve_dxcam_region(
    bbox: tuple[int, int, int, int],
) -> tuple[int, tuple[int, int, int, int] | None] | None:
    """Resolve a screen-coord bbox to ``(output_idx, region)`` for dxcam.

    Finds the first monitor (via :func:`GetMonitorsRect`, whose
    ``EnumDisplayMonitors`` order matches dxcam's ``output_idx``) that FULLY
    contains ``bbox`` and returns:
      - ``(output_idx, None)``      — bbox covers the whole monitor (full grab)
      - ``(output_idx, region)``    — region is bbox translated to the
        monitor's own origin (dxcam regions are monitor-relative)
      - ``None``                     — no single monitor contains bbox (it
        spans monitors or is off-screen); the caller should fall back to a
        virtual-screen-aware backend (mss/pillow).

    Adapted from Windows-MCP's ``_DxcamBackend._resolve_region``.
    """
    from agent_windows.uia.core import GetMonitorsRect

    bbox_left, bbox_top, bbox_right, bbox_bottom = bbox
    for output_idx, mon in enumerate(GetMonitorsRect()):
        if not (mon.left <= bbox_left
                and mon.top <= bbox_top
                and mon.right >= bbox_right
                and mon.bottom >= bbox_bottom):
            continue
        if (mon.left == bbox_left and mon.top == bbox_top
                and mon.right == bbox_right and mon.bottom == bbox_bottom):
            return output_idx, None  # bbox == whole monitor → full grab
        return output_idx, (
            bbox_left - mon.left,
            bbox_top - mon.top,
            bbox_right - mon.left,
            bbox_bottom - mon.top,
        )
    return None


def _get_dxcam_camera(output_idx: int):
    """Return a cached per-output dxcam camera (creating it on first use).

    Replaces the old single ``output_idx=0`` singleton: each monitor gets its
    own camera so a secondary-monitor bbox is captured from the correct DXGI
    output, not mis-grabbed from the primary adapter. (TODO #9-A)

    Caller MUST hold ``_dxcam_lock``: both call sites (``_capture_dxcam``) do,
    and the cache read-create-write here is not otherwise synchronized — a
    lock-free caller could double-create a camera for one output.
    """
    import dxcam
    camera = _dxcam_cameras.get(output_idx)
    if camera is None:
        camera = dxcam.create(output_idx=output_idx, processor_backend="numpy")
        _dxcam_cameras[output_idx] = camera
    return camera


def _capture_dxcam(bbox: tuple[int, int, int, int] | None) -> Image.Image:
    """Capture via dxcam (DXGI).

    With a bbox, the bbox is resolved to its owning monitor's output + a
    monitor-relative region (so secondary monitors capture correctly). With no
    bbox, dxcam captures the primary monitor only (``output_idx=0``) — a
    full virtual-screen capture is the job of mss/pillow, which the backend
    chain falls back to. (TODO #9-A)
    """
    with _dxcam_lock:
        if bbox is None:
            # No bbox → primary monitor full grab (unchanged behavior). A
            # full multi-monitor grab is mss/pillow's responsibility.
            camera = _get_dxcam_camera(0)
            frame = camera.grab(copy=True)
        else:
            resolved = _resolve_dxcam_region(bbox)
            if resolved is None:
                # bbox spans monitors or is off-screen — dxcam can't capture it
                # from a single output. Raise so capture_screenshot's auto-chain
                # falls back to mss/pillow (which use virtual-screen coords).
                raise ValueError(
                    "DXGI capture supports only regions fully contained "
                    "within one display")
            output_idx, region = resolved
            camera = _get_dxcam_camera(output_idx)
            frame = camera.grab(region=region, copy=True) if region is not None \
                else camera.grab(copy=True)
    if frame is None:
        raise RuntimeError("DXGI capture returned no frame")
    return Image.fromarray(frame)


def _capture_mss(bbox: tuple[int, int, int, int] | None) -> Image.Image:
    """Capture via mss — cross-platform, multi-monitor aware."""
    global _mss_instance
    import mss

    with _mss_lock:
        if _mss_instance is None:
            _mss_instance = mss.mss()
        sct = _mss_instance
        if bbox:
            monitor = {
                "left": bbox[0],
                "top": bbox[1],
                "width": bbox[2] - bbox[0],
                "height": bbox[3] - bbox[1],
            }
        else:
            monitor = sct.monitors[0]  # Full virtual screen
        raw = sct.grab(monitor)
    return Image.frombytes("RGB", raw.size, raw.rgb)


def _capture_pillow(bbox: tuple[int, int, int, int] | None) -> Image.Image:
    """Capture via Pillow ImageGrab — universal fallback."""
    from PIL import ImageGrab

    kwargs: dict[str, Any] = {"all_screens": True}
    if bbox:
        kwargs["bbox"] = bbox
    try:
        return ImageGrab.grab(**kwargs)
    except (OSError, RuntimeError, ValueError):
        if bbox:
            # Fallback: grab full screen then crop
            img = ImageGrab.grab(all_screens=True)
            return img.crop(bbox)
        # Last resort: primary monitor only
        return ImageGrab.grab()


BACKEND_CHAIN = [
    ("dxcam", _capture_dxcam),
    ("mss", _capture_mss),
    ("pillow", _capture_pillow),
]


def capture_screenshot(
    monitor: int = -1,
    bbox: tuple[int, int, int, int] | None = None,
) -> Image.Image:
    """Capture a screenshot using the configured backend chain.

    Args:
        monitor: Monitor index (-1 for all/entire virtual screen).
        bbox: Optional bounding box (left, top, right, bottom) in screen coords.

    Returns:
        PIL Image of the screenshot.
    """
    config = load_config()
    backend_config = config.get("screenshot", {}).get("backend", "auto")

    if bbox is None and monitor >= 0:
        # Build bbox for a specific monitor
        bbox = _monitor_bbox(monitor)

    global _last_successful_backend

    if backend_config == "auto":
        # Try last successful backend first
        if _last_successful_backend:
            for name, capture_fn in BACKEND_CHAIN:
                if name == _last_successful_backend:
                    try:
                        img = capture_fn(bbox)
                        logger.debug("Screenshot captured via %s (cached)", name)
                        return img
                    except Exception:
                        break
        # Full chain fallback
        for name, capture_fn in BACKEND_CHAIN:
            try:
                img = capture_fn(bbox)
                _last_successful_backend = name
                logger.debug("Screenshot captured via %s", name)
                return img
            except Exception as e:
                logger.debug("Backend %s failed: %s, trying next", name, e)
                continue
        # Hard fallback: record truthfully (TODO #3).
        _last_successful_backend = "pillow"
        return _capture_pillow(bbox)
    else:
        # Specific backend requested — record it too so a pinned-backend
        # config doesn't leak a stale name from a prior auto-chain capture.
        for name, capture_fn in BACKEND_CHAIN:
            if name == backend_config:
                _last_successful_backend = name
                return capture_fn(bbox)
        raise ValueError(f"Unknown screenshot backend: {backend_config}")


def save_screenshot(
    image: Image.Image,
    output_path: str | Path | None = None,
) -> Path:
    """Save a PIL Image to disk and return the path.

    Args:
        image: PIL Image to save.
        output_path: Optional explicit output path.

    Returns:
        Path to the saved file.
    """
    config = load_config()
    screenshot_config = config.get("screenshot", {})

    if output_path:
        path = Path(output_path)
    else:
        save_dir = get_screenshot_save_dir()
        ext = screenshot_config.get("format", "png")
        timestamp = int(time.time() * 1000)
        path = save_dir / f"screenshot-{timestamp}.{ext}"

    path.parent.mkdir(parents=True, exist_ok=True)

    fmt = path.suffix.lstrip(".").upper()
    if fmt == "JPG":
        fmt = "JPEG"

    save_kwargs: dict[str, Any] = {}
    if fmt == "JPEG":
        save_kwargs["quality"] = screenshot_config.get("jpeg_quality", 85)

    image.save(path, format=fmt, **save_kwargs)
    logger.debug("Screenshot saved: %s (%dx%d)", path, image.width, image.height)

    return path


def save_and_cleanup(image: Image.Image, output_path: str | None = None) -> Path:
    """Save image and trigger cleanup if interval allows.

    Convenience wrapper that combines save_screenshot + cleanup_screenshots,
    used by screenshot, highlight, diff, and grounding functions.
    """
    path = save_screenshot(image, output_path=output_path)
    try:
        cleanup_screenshots()
    except Exception:
        logger.debug("Post-save cleanup failed", exc_info=True)
    return path


def _get_resampling_filter() -> int:
    """Resolve the PIL resampling filter for saved screenshots from config.

    Controlled by [screenshot] resampling: defaults to BILINEAR (fast), since
    downscaled screenshots don't benefit from LANCZOS's extra cost. (H-05)

    Returns:
        Image.LANCZOS, Image.BILINEAR, or Image.NEAREST.
    """
    config = load_config()
    name = str(config.get("screenshot", {}).get("resampling", "BILINEAR")).upper()
    if name == "LANCZOS":
        return Image.LANCZOS
    if name == "NEAREST":
        return Image.NEAREST
    return Image.BILINEAR


def scale_screenshot(image: Image.Image, max_width: int = 0, max_height: int = 0, scale: float = 1.0) -> Image.Image:
    """Scale an image if needed, maintaining aspect ratio."""
    if scale == 1.0 and not max_width and not max_height:
        return image

    scale_w = max_width / image.width if max_width and image.width > max_width else 1.0
    scale_h = max_height / image.height if max_height and image.height > max_height else 1.0
    final_scale = min(scale, scale_w, scale_h)

    if final_scale < 1.0:
        new_size = (int(image.width * final_scale), int(image.height * final_scale))
        return image.resize(new_size, _get_resampling_filter())
    return image


def _monitor_bbox(monitor_index: int) -> tuple[int, int, int, int]:
    """Build bbox for a specific monitor (0-based index)."""
    import ctypes
    import ctypes.wintypes

    monitors: list[tuple[int, int, int, int]] = []

    def _callback(hMonitor, hdcMonitor, lprcMonitor, dwData):
        rect = lprcMonitor.contents
        monitors.append((rect.left, rect.top, rect.right, rect.bottom))
        return True

    MONITORENUMPROC = ctypes.WINFUNCTYPE(
        ctypes.c_int,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.POINTER(ctypes.wintypes.RECT),
        ctypes.wintypes.LPARAM,
    )

    ctypes.windll.user32.EnumDisplayMonitors(None, None, MONITORENUMPROC(_callback), 0)

    if monitor_index < 0 or monitor_index >= len(monitors):
        raise ValueError(f"Monitor index {monitor_index} out of range (0-{len(monitors) - 1})")

    return monitors[monitor_index]


# ── Screenshot annotation ────────────────────────────────────────────


def highlight_region(
    image: Image.Image,
    regions: list[dict],
    output_path: str | Path | None = None,
) -> Path:
    """Draw highlight rectangles with optional labels on a screenshot.

    Args:
        image: PIL Image to annotate.
        regions: List of dicts, each with:
            x1, y1, x2, y2 (int) — bounding box.
            label (str, optional) — text label.
            color (str, optional) — border color (default "red").
        output_path: Save path. If None, auto-generates.

    Returns:
        Path to the saved annotated image.
    """
    from PIL import ImageDraw, ImageFont

    if not regions:
        raise ValueError("At least one region is required")

    marked = image.copy()
    draw = ImageDraw.Draw(marked)

    try:
        font = ImageFont.load_default(size=14)
    except TypeError:
        font = ImageFont.load_default()

    for r in regions:
        x1, y1, x2, y2 = r["x1"], r["y1"], r["x2"], r["y2"]
        color = r.get("color", "red")

        # Draw border (width=3)
        draw.rectangle([(x1, y1), (x2, y2)], outline=color, width=3)

        # Draw label if provided
        label = r.get("label", "")
        if label:
            text_pos = (x1 + 3, y1 + 3)
            text_bbox = draw.textbbox(text_pos, label, font=font)
            text_bg = [
                text_bbox[0] - 1, text_bbox[1] - 1,
                text_bbox[2] + 1, text_bbox[3] + 1,
            ]
            draw.rectangle(text_bg, fill="white")
            draw.text(text_pos, label, fill="black", font=font)

    if output_path:
        path = Path(output_path)
    else:
        save_dir = get_screenshot_save_dir()
        timestamp = int(time.time() * 1000)
        path = save_dir / f"highlight-{timestamp}.png"

    return save_and_cleanup(marked, output_path=str(path))


def diff_images(
    image_a: Image.Image,
    image_b: Image.Image,
    threshold: int = 30,
    output_path: str | Path | None = None,
) -> dict[str, Any]:
    """Pixel-level diff between two screenshots.

    Args:
        image_a: Baseline image.
        image_b: Comparison image.
        threshold: Per-channel diff threshold (0-255). Pixels below this
            are considered identical.
        output_path: Save diff visualization. If None, auto-generates.

    Returns:
        Dict with: diff_pixels, total_pixels, diff_percent, match, diff_path.
    """
    from PIL import ImageChops

    # Resize B to match A if needed
    if image_b.size != image_a.size:
        image_b = image_b.resize(image_a.size, _get_resampling_filter())

    # Convert both to RGB
    a = image_a.convert("RGB")
    b = image_b.convert("RGB")

    # Raw per-channel difference → grayscale
    diff_gray = ImageChops.difference(a, b).convert("L")

    # numpy availability resolved once at module load (PERF3-08)
    if _HAS_NUMPY:
        import numpy as np
        mask_arr = np.array(diff_gray)
        diff_mask = mask_arr > threshold
        diff_pixels = int(np.count_nonzero(diff_mask))
        total_pixels = mask_arr.size

        # Build visualization with numpy
        vis = np.array(a)
        vis_rgba = np.dstack([vis, np.full((*vis.shape[:2], 1), 255, dtype=np.uint8)])

        # Red tint on diff pixels
        diff_coords = np.where(diff_mask)
        vis_rgba[diff_coords[0], diff_coords[1], 0] = 255  # R
        vis_rgba[diff_coords[0], diff_coords[1], 1] = 0    # G
        vis_rgba[diff_coords[0], diff_coords[1], 2] = 0    # B
        vis_rgba[diff_coords[0], diff_coords[1], 3] = 180  # A

        # Outline via contour filter
        from PIL import ImageFilter
        outline = diff_gray.point(lambda p: 255 if p > threshold else 0)
        outline = outline.filter(ImageFilter.CONTOUR)
        outline_arr = np.array(outline)
        outline_coords = np.where(outline_arr > 0)
        vis_rgba[outline_coords[0], outline_coords[1], 0] = 255
        vis_rgba[outline_coords[0], outline_coords[1], 1] = 0
        vis_rgba[outline_coords[0], outline_coords[1], 2] = 0
        vis_rgba[outline_coords[0], outline_coords[1], 3] = 255

        vis_img = Image.fromarray(vis_rgba, mode="RGBA")
    else:
        # Fallback without numpy: simple side-by-side diff
        mask = diff_gray.point(lambda p: 255 if p > threshold else 0)
        diff_pixels = sum(1 for v in mask.getchannel("L").tobytes() if v > 0)
        total_pixels = mask.width * mask.height
        vis_img = ImageChops.difference(a, b)

    diff_percent = round(diff_pixels / max(total_pixels, 1) * 100, 2)

    if output_path:
        path = Path(output_path)
    else:
        save_dir = get_screenshot_save_dir()
        timestamp = int(time.time() * 1000)
        path = save_dir / f"diff-{timestamp}.png"

    save_and_cleanup(vis_img, output_path=str(path))

    return {
        "diff_pixels": diff_pixels,
        "total_pixels": total_pixels,
        "diff_percent": diff_percent,
        "match": diff_pixels == 0,
        "diff_path": str(path),
    }


# ── Screenshot cleanup ────────────────────────────────────────────────

_last_cleanup_time: float = 0.0
_cleanup_lock = threading.Lock()


def cleanup_screenshots(force: bool = False) -> dict[str, Any]:
    """Clean screenshots by age and size.

    Three trigger points:
    1. Daemon startup (force=True)
    2. After each screenshot save (force=False, respects interval)
    3. Explicit CLI command (force=True)

    Args:
        force: If True, skip interval check and run immediately.

    Returns:
        Dict with cleanup stats: deleted_by_age, deleted_by_size,
        total_kept, dir_size_mb, skipped.
    """
    global _last_cleanup_time

    config = load_config()
    ss_config = config.get("screenshot", {})

    # Interval check (skip when force=True)
    interval = ss_config.get("cleanup_interval_minutes", 10)
    if not force and interval > 0:
        with _cleanup_lock:
            if time.monotonic() - _last_cleanup_time < interval * 60:
                return {"skipped": True}
            _last_cleanup_time = time.monotonic()
    else:
        with _cleanup_lock:
            _last_cleanup_time = time.monotonic()

    stats: dict[str, Any] = {
        "deleted_by_age": 0,
        "deleted_by_size": 0,
        "total_kept": 0,
        "dir_size_mb": 0.0,
        "skipped": False,
    }

    save_dir = get_screenshot_save_dir()
    if not save_dir.exists():
        return stats

    # Collect files with mtime and size
    files: list[tuple[Path, float, int]] = []
    for f in save_dir.iterdir():
        if f.is_file():
            try:
                st = f.stat()
                files.append((f, st.st_mtime, st.st_size))
            except OSError:
                continue

    if not files:
        return stats

    # --- Strategy 1: Age-based ---
    max_age_days = ss_config.get("max_screenshot_age_days", 7)
    if max_age_days > 0:
        cutoff = time.time() - max_age_days * 86400
        aged = [(f, mt, sz) for f, mt, sz in files if mt < cutoff]
        for f, _, _ in aged:
            try:
                f.unlink()
                stats["deleted_by_age"] += 1
            except OSError:
                pass
        aged_set = {f for f, _, _ in aged}
        files = [(f, mt, sz) for f, mt, sz in files if f not in aged_set]

    # --- Strategy 2: Size-based ---
    max_mb = ss_config.get("max_screenshot_dir_mb", 500)
    if max_mb > 0:
        total_size = sum(sz for _, _, sz in files)
        max_bytes = max_mb * 1024 * 1024
        if total_size > max_bytes:
            # Sort oldest first, delete until under limit
            files.sort(key=lambda x: x[1])
            for f, _, sz in files:
                if total_size <= max_bytes:
                    break
                try:
                    f.unlink()
                    total_size -= sz
                    stats["deleted_by_size"] += 1
                except OSError:
                    pass
            files = [(f, mt, sz) for f, mt, sz in files if f.exists()]

    stats["total_kept"] = len(files)
    stats["dir_size_mb"] = round(sum(sz for _, _, sz in files) / 1024 / 1024, 2)

    if stats["deleted_by_age"] or stats["deleted_by_size"]:
        logger.info(
            "Cleanup: %d by age, %d by size, %d kept (%.2f MB)",
            stats["deleted_by_age"], stats["deleted_by_size"],
            stats["total_kept"], stats["dir_size_mb"],
        )

    return stats


def clean_old_screenshots() -> int:
    """Legacy wrapper: force cleanup, return total deleted count."""
    result = cleanup_screenshots(force=True)
    return result.get("deleted_by_age", 0) + result.get("deleted_by_size", 0)
