"""Tests for the slimmed-down observe handler (Task 5).

observe is now a pure snapshot+screenshot bundle — the toggles and the ground
branch are gone, and ``use_annotation``/``width/height_reference_line`` forward
to the screenshot sub-call. These tests mock only ``_run_sub`` (the per-sub-
command route_request wrapper) so they exercise the real ``_observe`` body.
"""


def test_observe_always_snapshot_plus_screenshot_and_forwards_annotate(monkeypatch):
    from agent_windows.daemon.handlers import observe as obs

    calls = []
    def fake_run_sub(method, params):
        calls.append((method, dict(params)))
        return (f"[{method} text]", None)
    monkeypatch.setattr(obs, "_run_sub", fake_run_sub)

    # default: both sub-calls, no annotate
    obs._observe({})
    assert [c[0] for c in calls] == ["snapshot", "screenshot"]
    assert "use_annotation" not in calls[1][1]

    calls.clear()
    obs._observe({"use_annotation": True, "width_reference_line": 10})
    assert [c[0] for c in calls] == ["snapshot", "screenshot"]
    assert calls[1][1]["use_annotation"] is True
    assert calls[1][1]["width_reference_line"] == 10
    assert "use_annotation" not in calls[0][1]   # snapshot unaffected


def test_observe_ignores_legacy_ground_param(monkeypatch):
    """--ground is dropped; observe is a pure snapshot+screenshot bundle now."""
    from agent_windows.daemon.handlers import observe as obs
    calls = []
    monkeypatch.setattr(obs, "_run_sub",
                        lambda method, params: calls.append(method) or ("t", None))
    obs._observe({"ground": "the taskbar"})
    assert calls == ["snapshot", "screenshot"]   # no third 'ground' sub-call


def test_observe_all_failed_raises(monkeypatch):
    from agent_windows.daemon.handlers import observe as obs
    import pytest
    monkeypatch.setattr(obs, "_run_sub",
                        lambda method, params: (f"[{method} failed: boom]",
                                                 {"method": method, "code": -1, "message": "boom"}))
    with pytest.raises(RuntimeError, match="every sub-command failed"):
        obs._observe({})
