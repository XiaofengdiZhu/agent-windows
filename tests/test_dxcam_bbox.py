"""Tests for the dxcam per-output region resolution + camera cache (TODO #9-A).

The dxcam backend previously hardcoded ``output_idx=0`` (single singleton),
which mis-grabbed secondary-monitor bboxes from the primary adapter. The fix
resolves a bbox to its OWNING monitor via ``GetMonitorsRect`` (whose
``EnumDisplayMonitors`` order matches dxcam's ``output_idx``) and caches a
camera per output index.

These tests inject a fake ``dxcam`` module and mock ``GetMonitorsRect`` to
control monitor layout (including negative origins for monitors left/above the
primary), then assert the exact ``output_idx`` passed to ``dxcam.create`` and
the ``region=`` passed to ``camera.grab``. No real DXGI, no real monitor,
fully deterministic.
"""
import sys
from unittest.mock import MagicMock, patch

import pytest

from agent_windows.uia.core import Rect


def _mod():
    import agent_windows.core.screenshot as ss
    return ss


def _monitors(*rects):
    """Build a list of Rect objects for GetMonitorsRect to return."""
    return [Rect(l, t, r, b) for (l, t, r, b) in rects]


@pytest.fixture
def fake_dxcam(monkeypatch):
    """Inject a fake dxcam module so `import dxcam` inside _get_dxcam_camera
    resolves to a MagicMock. Returns the fake module so tests can wire
    create()/grab() return values."""
    fake = MagicMock(name="dxcam")
    monkeypatch.setitem(sys.modules, "dxcam", fake)
    ss = _mod()
    # Reset the per-output camera cache so each test re-creates cameras from
    # the fake. (TODO #9-A: _dxcam_camera singleton → _dxcam_cameras dict.)
    saved = ss._dxcam_cameras
    ss._dxcam_cameras = {}
    yield fake
    ss._dxcam_cameras = saved


def _wire_camera(fake_module, grab_return):
    """Make fake dxcam.create() return a fresh camera whose .grab() yields
    grab_return, regardless of output_idx. Each create() call returns its OWN
    MagicMock so per-output cameras are distinct (TODO #9-A)."""
    def _create(output_idx, processor_backend):
        cam = MagicMock(name=f"dxcam_camera_{output_idx}")
        cam.grab.return_value = grab_return
        return cam
    fake_module.create.side_effect = _create
    # Return the most-recently-created camera is not meaningful with side_effect;
    # tests that need a specific camera assert on _dxcam_cameras[output_idx].
    return fake_module


def _patch_monitors(monitors):
    """Patch GetMonitorsRect inside _resolve_dxcam_region (imported lazily from
    agent_windows.uia.core) to return the given list of Rect."""
    return patch("agent_windows.uia.core.GetMonitorsRect", return_value=monitors)


# ── _resolve_dxcam_region (TODO #9-A) ─────────────────────────────────


class TestResolveRegion:
    """The pure region resolver: bbox → (output_idx, region) | None."""

    def test_bbox_on_primary_returns_idx0(self):
        ss = _mod()
        monitors = _monitors((0, 0, 1920, 1080))
        with _patch_monitors(monitors):
            result = ss._resolve_dxcam_region((100, 200, 500, 600))
        assert result == (0, (100, 200, 500, 600))

    def test_bbox_on_secondary_returns_idx1(self):
        """The core multi-monitor fix: a bbox on the second monitor resolves to
        output_idx=1 with a region relative to THAT monitor's origin."""
        ss = _mod()
        monitors = _monitors((0, 0, 1920, 1080), (1920, 0, 3840, 1080))
        with _patch_monitors(monitors):
            result = ss._resolve_dxcam_region((2000, 100, 2400, 500))
        # region = bbox - monitor1 origin (1920, 0) = (80, 100, 480, 500)
        assert result == (1, (80, 100, 480, 500))

    def test_bbox_equals_whole_monitor_returns_none_region(self):
        """bbox covering an entire monitor → region=None (full grab, no crop)."""
        ss = _mod()
        monitors = _monitors((0, 0, 1920, 1080), (1920, 0, 3840, 1080))
        with _patch_monitors(monitors):
            result = ss._resolve_dxcam_region((1920, 0, 3840, 1080))
        assert result == (1, None)

    def test_bbox_spanning_monitors_returns_none(self):
        """A bbox crossing two monitors is not contained in any single one → None
        (caller falls back to mss/pillow)."""
        ss = _mod()
        monitors = _monitors((0, 0, 1920, 1080), (1920, 0, 3840, 1080))
        with _patch_monitors(monitors):
            result = ss._resolve_dxcam_region((1800, 100, 2000, 500))
        assert result is None

    def test_bbox_offscreen_returns_none(self):
        ss = _mod()
        with _patch_monitors(_monitors((0, 0, 1920, 1080))):
            result = ss._resolve_dxcam_region((-500, -500, -100, -100))
        assert result is None

    def test_negative_origin_monitor(self):
        """Secondary monitor to the LEFT of primary (origin negative)."""
        ss = _mod()
        monitors = _monitors((-1920, 0, 0, 1080), (0, 0, 1920, 1080))
        with _patch_monitors(monitors):
            result = ss._resolve_dxcam_region((-1920, 0, -1024, 768))
        # region = bbox - monitor0 origin (-1920, 0) = (0, 0, 896, 768)
        assert result == (0, (0, 0, 896, 768))


# ── _capture_dxcam: bbox → correct output + region ───────────────────


class TestCaptureRoutesToCorrectOutput:
    def test_primary_bbox_uses_output_zero(self, fake_dxcam):
        ss = _mod()
        _wire_camera(fake_dxcam, grab_return=[[1, 2, 3]])
        with _patch_monitors(_monitors((0, 0, 1920, 1080))), \
             patch("PIL.Image.fromarray", return_value=MagicMock()):
            ss._capture_dxcam(bbox=(100, 200, 500, 600))
        fake_dxcam.create.assert_called_once_with(
            output_idx=0, processor_backend="numpy")
        ss._dxcam_cameras[0].grab.assert_called_once_with(
            region=(100, 200, 500, 600), copy=True)

    def test_secondary_bbox_uses_output_one(self, fake_dxcam):
        """The bug being fixed: a secondary-monitor bbox must capture from
        output_idx=1, not the hardcoded primary adapter."""
        ss = _mod()
        _wire_camera(fake_dxcam, grab_return=[[1]])
        monitors = _monitors((0, 0, 1920, 1080), (1920, 0, 3840, 1080))
        with _patch_monitors(monitors), \
             patch("PIL.Image.fromarray", return_value=MagicMock()):
            ss._capture_dxcam(bbox=(2000, 100, 2400, 500))
        fake_dxcam.create.assert_called_once_with(
            output_idx=1, processor_backend="numpy")
        # region relative to monitor 1 origin (1920, 0)
        ss._dxcam_cameras[1].grab.assert_called_once_with(
            region=(80, 100, 480, 500), copy=True)
        # primary camera NOT created (per-output isolation)
        assert 0 not in ss._dxcam_cameras

    def test_bbox_equal_whole_monitor_grabs_without_region(self, fake_dxcam):
        """bbox == whole monitor → region=None → grab() with no region kwarg."""
        ss = _mod()
        _wire_camera(fake_dxcam, grab_return=[[1]])
        with _patch_monitors(_monitors((0, 0, 1920, 1080))), \
             patch("PIL.Image.fromarray", return_value=MagicMock()):
            ss._capture_dxcam(bbox=(0, 0, 1920, 1080))
        ss._dxcam_cameras[0].grab.assert_called_once_with(copy=True)


# ── no-bbox path ─────────────────────────────────────────────────────


class TestNoBboxPath:
    def test_no_bbox_grabs_full_primary(self, fake_dxcam):
        """No bbox → grab the whole primary adapter (output_idx=0, no region)."""
        ss = _mod()
        _wire_camera(fake_dxcam, grab_return=[[1]])
        with patch("PIL.Image.fromarray", return_value=MagicMock()):
            ss._capture_dxcam(bbox=None)
        fake_dxcam.create.assert_called_once_with(
            output_idx=0, processor_backend="numpy")
        ss._dxcam_cameras[0].grab.assert_called_once_with(copy=True)

    def test_no_bbox_does_not_enumerate_monitors(self, fake_dxcam):
        """The no-bbox path must NOT call GetMonitorsRect (no resolution needed).
        Locks that the monitor lookup is gated behind the bbox branch."""
        ss = _mod()
        _wire_camera(fake_dxcam, grab_return=[[1]])
        with _patch_monitors(_monitors()) as mock_monitors, \
             patch("PIL.Image.fromarray", return_value=MagicMock()):
            ss._capture_dxcam(bbox=None)
        mock_monitors.assert_not_called()


# ── per-output camera cache (TODO #9-A) ──────────────────────────────


class TestCameraCache:
    def test_same_output_reuses_camera(self, fake_dxcam):
        """Two captures on the same output reuse one camera (create called once)."""
        ss = _mod()
        _wire_camera(fake_dxcam, grab_return=[[1]])
        with _patch_monitors(_monitors((0, 0, 1920, 1080))), \
             patch("PIL.Image.fromarray", return_value=MagicMock()):
            ss._capture_dxcam(bbox=(100, 200, 500, 600))
            ss._capture_dxcam(bbox=(100, 200, 500, 600))
        fake_dxcam.create.assert_called_once()

    def test_different_outputs_each_get_own_camera(self, fake_dxcam):
        """Captures on output 0 and output 1 create two distinct cameras."""
        ss = _mod()
        _wire_camera(fake_dxcam, grab_return=[[1]])
        monitors = _monitors((0, 0, 1920, 1080), (1920, 0, 3840, 1080))
        with _patch_monitors(monitors), \
             patch("PIL.Image.fromarray", return_value=MagicMock()):
            ss._capture_dxcam(bbox=(100, 200, 500, 600))      # primary
            ss._capture_dxcam(bbox=(2000, 100, 2400, 500))    # secondary
        assert fake_dxcam.create.call_count == 2
        assert set(ss._dxcam_cameras.keys()) == {0, 1}
        # The two cameras are distinct objects (per-output isolation), not the
        # same MagicMock returned twice.
        assert ss._dxcam_cameras[0] is not ss._dxcam_cameras[1]


# ── cross-screen fallback ────────────────────────────────────────────


class TestCrossScreenFallback:
    def test_cross_screen_bbox_raises(self, fake_dxcam):
        """A bbox spanning monitors makes _resolve_dxcam_region return None →
        _capture_dxcam raises so capture_screenshot's auto-chain falls back to
        mss/pillow (which handle virtual-screen coords correctly)."""
        ss = _mod()
        _wire_camera(fake_dxcam, grab_return=[[1]])
        monitors = _monitors((0, 0, 1920, 1080), (1920, 0, 3840, 1080))
        with _patch_monitors(monitors), \
             patch("PIL.Image.fromarray", return_value=MagicMock()):
            with pytest.raises(ValueError, match="fully contained within one display"):
                ss._capture_dxcam(bbox=(1800, 100, 2000, 500))

    def test_capture_screenshot_falls_back_to_mss(self, fake_dxcam):
        """End-to-end: a cross-screen bbox fails dxcam → auto-chain tries mss.
        Patches BACKEND_CHAIN (bound at import) like TestCaptureBackendRecording."""
        ss = _mod()
        ss._last_successful_backend = None
        monitors = _monitors((0, 0, 1920, 1080), (1920, 0, 3840, 1080))
        dxcam_fn = MagicMock(side_effect=ValueError("cross-screen"))
        mss_fn = MagicMock(return_value=MagicMock(name="mss_image"))
        fake_chain = [("dxcam", dxcam_fn), ("mss", mss_fn),
                      ("pillow", MagicMock())]
        with _patch_monitors(monitors), \
             patch.object(ss, "BACKEND_CHAIN", fake_chain), \
             patch("agent_windows.utils.config.load_config") as m_cfg:
            m_cfg.return_value = {"screenshot": {"backend": "auto"}}
            img = ss.capture_screenshot(bbox=(1800, 100, 2000, 500))
        dxcam_fn.assert_called_once()
        mss_fn.assert_called_once()
        assert ss.get_capture_backend() == "mss"


# ── failure path ─────────────────────────────────────────────────────


class TestGrabFailure:
    def test_none_frame_raises_runtime_error(self, fake_dxcam):
        """grab() returning None (no frame) → RuntimeError, not a silent None
        return or a crash in Image.fromarray."""
        ss = _mod()
        _wire_camera(fake_dxcam, grab_return=None)
        with patch("PIL.Image.fromarray", return_value=MagicMock()):
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
