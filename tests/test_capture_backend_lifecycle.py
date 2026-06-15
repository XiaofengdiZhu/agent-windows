"""Tests for capture-backend singleton release (L-29).

dxcam/mss singletons were never released on exit. release_capture_backends()
closes mss and drops the dxcam reference, and is registered with atexit so every
exit path runs it. These tests exercise the function directly (atexit itself is
exercised by process teardown, not unit-testable here).
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


def test_release_clears_dxcam_ref():
    ss = _mod()
    saved_cam = ss._dxcam_camera
    ss._dxcam_camera = MagicMock()
    try:
        ss.release_capture_backends()
        assert ss._dxcam_camera is None
    finally:
        ss._dxcam_camera = saved_cam


def test_release_idempotent():
    ss = _mod()
    ss._mss_instance = None
    ss._dxcam_camera = None
    ss.release_capture_backends()  # already-released state must not error
    assert ss._mss_instance is None
    assert ss._dxcam_camera is None


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
