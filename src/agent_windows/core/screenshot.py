"""Screenshot service: multi-backend capture (dxcam → mss → Pillow).

Auto-selects the best available backend. Saves to disk and returns path info.
"""

import logging
import time
from pathlib import Path
from typing import Any

from PIL import Image

from agent_windows.utils.config import load_config, get_screenshot_save_dir

logger = logging.getLogger(__name__)

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


def get_primary_monitor() -> int:
    """Return 1-based index of primary monitor (always 1 for API consistency)."""
    return 1


# ── Backend capture functions ─────────────────────────────────────


def _capture_dxcam(bbox: tuple[int, int, int, int] | None) -> Image.Image:
    """Capture via dxcam (DXGI) — fast but single-monitor only."""
    import dxcam

    camera = dxcam.create(output_idx=0, processor_backend="numpy")
    if bbox:
        # dxcam region is monitor-local; translate from virtual coords
        left, top, width, height = get_virtual_screen_rect()
        region = (bbox[0] - left, bbox[1] - top, bbox[2] - left, bbox[3] - top)
        frame = camera.grab(region=region, copy=True)
    else:
        frame = camera.grab(copy=True)
    if frame is None:
        raise RuntimeError("DXGI capture returned no frame")
    return Image.fromarray(frame)


def _capture_mss(bbox: tuple[int, int, int, int] | None) -> Image.Image:
    """Capture via mss — cross-platform, multi-monitor aware."""
    import mss

    with mss.mss() as sct:
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

    if backend_config == "auto":
        for name, capture_fn in BACKEND_CHAIN:
            try:
                img = capture_fn(bbox)
                logger.debug("Screenshot captured via %s", name)
                return img
            except Exception as e:
                logger.debug("Backend %s failed: %s, trying next", name, e)
                continue
        # Hard fallback to Pillow
        return _capture_pillow(bbox)
    else:
        # Specific backend requested
        for name, capture_fn in BACKEND_CHAIN:
            if name == backend_config:
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


def scale_screenshot(image: Image.Image, max_width: int = 0, max_height: int = 0, scale: float = 1.0) -> Image.Image:
    """Scale an image if needed, maintaining aspect ratio."""
    if scale == 1.0 and not max_width and not max_height:
        return image

    scale_w = max_width / image.width if max_width and image.width > max_width else 1.0
    scale_h = max_height / image.height if max_height and image.height > max_height else 1.0
    final_scale = min(scale, scale_w, scale_h)

    if final_scale < 1.0:
        new_size = (int(image.width * final_scale), int(image.height * final_scale))
        return image.resize(new_size, Image.LANCZOS)
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


def clean_old_screenshots() -> int:
    """Remove screenshots older than 7 days. Returns count deleted."""
    import time

    save_dir = get_screenshot_save_dir()
    if not save_dir.exists():
        return 0

    config = load_config()
    max_age_days = config.get("screenshot", {}).get("max_screenshot_age_days", 7)
    cutoff = time.time() - max_age_days * 86400
    deleted = 0

    for f in save_dir.iterdir():
        if f.is_file() and f.stat().st_mtime < cutoff:
            f.unlink()
            deleted += 1

    return deleted
