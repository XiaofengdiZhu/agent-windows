"""Tests for capture-backend release (L-29).

dxcam/mss backends hold DXGI / screen-DC resources that were leaked on exit.
release_capture_backends() closes mss and drops the per-output dxcam camera
references, and is registered with atexit so every exit path runs it. These
tests exercise the function directly (atexit itself is exercised by process
teardown, not unit-testable here).
"""
from unittest.mock import MagicMock


def _mod():
    import agent_windows.core.screenshot as ss
    return ss


def test_release_closes_and_clears_mss():
    ss = _mod()
    fake_mss = MagicMock()
    saved = ss._mss_instance
    ss._mss_instance = fake_mss
    try:
        ss.release_capture_backends()
        fake_mss.close.assert_called_once()
        assert ss._mss_instance is None
    finally:
        ss._mss_instance = saved


def test_release_clears_dxcam_cameras():
    ss = _mod()
    saved_cams = ss._dxcam_cameras
    ss._dxcam_cameras = {0: MagicMock(), 1: MagicMock()}
    try:
        ss.release_capture_backends()
        assert ss._dxcam_cameras == {}
    finally:
        ss._dxcam_cameras = saved_cams


def test_release_idempotent():
    ss = _mod()
    ss._mss_instance = None
    ss._dxcam_cameras = {}
    ss.release_capture_backends()  # already-released state must not error
    assert ss._mss_instance is None
    assert ss._dxcam_cameras == {}


def test_release_swallows_mss_close_error():
    """A failing mss.close() must not prevent clearing the singleton."""
    ss = _mod()
    bad = MagicMock()
    bad.close.side_effect = OSError("boom")
    saved = ss._mss_instance
    ss._mss_instance = bad
    try:
        ss.release_capture_backends()  # must not raise
        assert ss._mss_instance is None
    finally:
        ss._mss_instance = saved
