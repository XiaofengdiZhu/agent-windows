"""Tests for the dxcam bbox→region virtual-screen offset logic (TEST3-05).

The dxcam backend is single-adapter (output_idx=0, PERF-03), so multi-monitor
coverage comes from mss/pillow. But _capture_dxcam DOES translate a screen-coord
bbox into a region by subtracting the virtual-screen origin (which is NEGATIVE
when a secondary monitor sits left/above the primary). That offset math
(screenshot.py:101-104) was completely untested — a regression forgetting the
`- left` / `- top` would silently grab the wrong region.

These tests inject a fake `dxcam` module into sys.modules and mock
get_virtual_screen_rect to control the origin (including negative), then assert
the exact `region=` passed to `camera.grab`. No real DXGI, no real monitor,
fully deterministic.
"""
import sys
from unittest.mock import MagicMock, patch

import pytest


def _mod():
    import agent_windows.core.screenshot as ss
    return ss


@pytest.fixture
def fake_dxcam(monkeypatch):
    """Inject a fake dxcam module so `import dxcam` inside _capture_dxcam
    resolves to a MagicMock. Returns the fake module so tests can wire
    create()/grab() return values."""
    fake = MagicMock(name="dxcam")
    monkeypatch.setitem(sys.modules, "dxcam", fake)
    ss = _mod()
    # Reset the singleton so each test re-creates the camera from the fake.
    saved = ss._dxcam_camera
    ss._dxcam_camera = None
    yield fake
    ss._dxcam_camera = saved


def _wire_camera(fake_module, grab_return):
    """Make fake dxcam.create() return a camera whose .grab() yields grab_return."""
    camera = MagicMock(name="dxcam_camera")
    camera.grab.return_value = grab_return
    fake_module.create.return_value = camera
    return camera


# ── bbox→region translation ──────────────────────────────────────────


class TestBboxRegionTranslation:
    def test_bbox_subtracts_origin(self, fake_dxcam):
        """A bbox in screen coords is translated to a region by subtracting the
        virtual-screen origin. With origin (0,0) the subtraction is an identity
        (this is a sanity check; the negative/positive-origin tests below are
        the load-bearing ones that catch a dropped `- left`/`- top`)."""
        ss = _mod()
        camera = _wire_camera(fake_dxcam, grab_return=[[1, 2, 3]])
        with patch.object(ss, "get_virtual_screen_rect", return_value=(0, 0, 1920, 1080)):
            with patch("PIL.Image.fromarray", return_value=MagicMock()):
                ss._capture_dxcam(bbox=(100, 200, 500, 600))
        # region = (bbox.l - vscreen.l, bbox.t - vscreen.t, bbox.r - vscreen.l, bbox.b - vscreen.t)
        camera.grab.assert_called_once_with(region=(100, 200, 500, 600), copy=True)

    def test_negative_origin_shifts_region_positive(self, fake_dxcam):
        """Secondary monitor to the LEFT of primary → virtual-screen left is
        NEGATIVE. A screen-coord bbox must shift INTO the positive region space.
        This is the core case that breaks if `- left` is dropped."""
        ss = _mod()
        camera = _wire_camera(fake_dxcam, grab_return=[[1]])
        # Virtual screen starts at x=-1920 (secondary on the left, 3840 wide).
        vscreen = (-1920, 0, 3840, 1080)
        # A bbox over the left monitor at screen coords (-1920,0)-(-1024,768).
        bbox = (-1920, 0, -1024, 768)
        with patch.object(ss, "get_virtual_screen_rect", return_value=vscreen):
            with patch("PIL.Image.fromarray", return_value=MagicMock()):
                ss._capture_dxcam(bbox=bbox)
        # region = (bbox - origin): (-1920-(-1920), 0-0, -1024-(-1920), 768-0)
        #        = (0, 0, 896, 768)
        camera.grab.assert_called_once_with(region=(0, 0, 896, 768), copy=True)

    def test_negative_top_origin(self, fake_dxcam):
        """Secondary monitor ABOVE primary → virtual-screen top negative."""
        ss = _mod()
        camera = _wire_camera(fake_dxcam, grab_return=[[1]])
        vscreen = (0, -1080, 1920, 2160)
        bbox = (100, -500, 400, -100)  # on the upper monitor
        with patch.object(ss, "get_virtual_screen_rect", return_value=vscreen):
            with patch("PIL.Image.fromarray", return_value=MagicMock()):
                ss._capture_dxcam(bbox=bbox)
        # region = (100-0, -500-(-1080), 400-0, -100-(-1080)) = (100, 580, 400, 980)
        camera.grab.assert_called_once_with(region=(100, 580, 400, 980), copy=True)

    def test_positive_origin_shifts_region_negative(self, fake_dxcam):
        """If the primary is itself offset (unusual but possible), origin is
        positive and region shifts negative — verifies the subtraction is
        symmetric, not hardcoded to `+ abs(left)`."""
        ss = _mod()
        camera = _wire_camera(fake_dxcam, grab_return=[[1]])
        with patch.object(ss, "get_virtual_screen_rect", return_value=(100, 50, 1920, 1080)):
            with patch("PIL.Image.fromarray", return_value=MagicMock()):
                ss._capture_dxcam(bbox=(150, 100, 300, 200))
        # region = (150-100, 100-50, 300-100, 200-50) = (50, 50, 200, 150)
        camera.grab.assert_called_once_with(region=(50, 50, 200, 150), copy=True)


# ── no-bbox path ─────────────────────────────────────────────────────


class TestNoBboxPath:
    def test_no_bbox_grabs_full_primary(self, fake_dxcam):
        """No bbox → grab the whole primary adapter (no region arg), locking the
        dxcam single-monitor default (PERF-03)."""
        ss = _mod()
        camera = _wire_camera(fake_dxcam, grab_return=[[1]])
        with patch("PIL.Image.fromarray", return_value=MagicMock()):
            ss._capture_dxcam(bbox=None)
        # No region kwarg → full primary grab
        camera.grab.assert_called_once_with(copy=True)

    def test_no_bbox_does_not_query_virtual_screen(self, fake_dxcam):
        """The no-bbox path must NOT call get_virtual_screen_rect (no translation
        needed). Locks that the origin lookup is gated behind `if bbox`."""
        ss = _mod()
        _wire_camera(fake_dxcam, grab_return=[[1]])
        with patch.object(ss, "get_virtual_screen_rect") as mock_vs:
            with patch("PIL.Image.fromarray", return_value=MagicMock()):
                ss._capture_dxcam(bbox=None)
        mock_vs.assert_not_called()


# ── camera creation: output_idx=0 (PERF-03) + singleton ──────────────


class TestCameraCreation:
    def test_camera_created_with_output_idx_zero(self, fake_dxcam):
        """dxcam.create is called with output_idx=0 — documents the PERF-03
        limitation: dxcam only ever captures the primary adapter."""
        ss = _mod()
        _wire_camera(fake_dxcam, grab_return=[[1]])
        with patch("PIL.Image.fromarray", return_value=MagicMock()):
            ss._capture_dxcam(bbox=None)
        fake_dxcam.create.assert_called_once_with(output_idx=0, processor_backend="numpy")

    def test_camera_cached_across_calls(self, fake_dxcam):
        """The singleton _dxcam_camera is reused; dxcam.create called once across
        multiple captures."""
        ss = _mod()
        _wire_camera(fake_dxcam, grab_return=[[1]])
        with patch("PIL.Image.fromarray", return_value=MagicMock()):
            ss._capture_dxcam(bbox=None)
            ss._capture_dxcam(bbox=None)
            ss._capture_dxcam(bbox=None)
        fake_dxcam.create.assert_called_once()


# ── failure path ─────────────────────────────────────────────────────


class TestGrabFailure:
    def test_none_frame_raises_runtime_error(self, fake_dxcam):
        """grab() returning None (no frame) → RuntimeError, not a silent None
        return or a crash in Image.fromarray."""
        ss = _mod()
        _wire_camera(fake_dxcam, grab_return=None)
        with pytest.raises(RuntimeError, match="no frame"):
            ss._capture_dxcam(bbox=None)


# ── capture_screenshot backend recording (TODO #3) ──────────────────────


@pytest.fixture
def reset_backend():
    """Reset _last_successful_backend for the test, restore it after (module
    global; without this a prior test's capture would leak in)."""
    ss = _mod()
    saved = ss._last_successful_backend
    ss._last_successful_backend = None
    yield ss
    ss._last_successful_backend = saved


class TestCaptureBackendRecording:
    """get_capture_backend() reports the backend that served the most recent
    capture. Drives the real capture_screenshot (patching BACKEND_CHAIN, which
    binds the _capture_* fns at import time)."""

    def test_explicit_backend_is_recorded(self, reset_backend):
        ss = reset_backend
        fake_chain = [("dxcam", MagicMock(side_effect=RuntimeError)),
                      ("mss", MagicMock(return_value=MagicMock())),
                      ("pillow", MagicMock(return_value=MagicMock()))]
        with patch("agent_windows.utils.config.load_config") as m_cfg, \
             patch.object(ss, "BACKEND_CHAIN", fake_chain):
            m_cfg.return_value = {"screenshot": {"backend": "mss"}}
            ss.capture_screenshot()
        assert ss.get_capture_backend() == "mss"

    def test_auto_chain_records_successful_backend(self, reset_backend):
        ss = reset_backend
        fake_chain = [("dxcam", MagicMock(return_value=MagicMock())),
                      ("mss", MagicMock(return_value=MagicMock())),
                      ("pillow", MagicMock(return_value=MagicMock()))]
        with patch("agent_windows.utils.config.load_config") as m_cfg, \
             patch.object(ss, "BACKEND_CHAIN", fake_chain):
            m_cfg.return_value = {"screenshot": {"backend": "auto"}}
            ss.capture_screenshot()
        assert ss.get_capture_backend() == "dxcam"

    def test_hard_fallback_to_pillow_is_recorded(self, reset_backend):
        """auto-chain with ALL backends failing → hard Pillow fallback runs and
        is recorded as "pillow" (not left stale on a failed backend). Patches
        _capture_pillow directly since the fallback calls it by name."""
        ss = reset_backend
        fake_chain = [("dxcam", MagicMock(side_effect=RuntimeError)),
                      ("mss", MagicMock(side_effect=RuntimeError)),
                      ("pillow", MagicMock(side_effect=RuntimeError))]
        with patch("agent_windows.utils.config.load_config") as m_cfg, \
             patch.object(ss, "BACKEND_CHAIN", fake_chain), \
             patch.object(ss, "_capture_pillow", return_value=MagicMock()):
            m_cfg.return_value = {"screenshot": {"backend": "auto"}}
            ss.capture_screenshot()
        assert ss.get_capture_backend() == "pillow"
