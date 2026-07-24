"""Unit tests for core/uia_bypass.py — no live desktop required."""

import pytest

import agent_windows.core.uia_bypass as bypass


def test_run_with_uia_bypass_disables_then_restores(monkeypatch):
    calls = []

    def fake_enable_window(hwnd, enable):
        calls.append((hwnd, enable))
        return 1  # previous state: was enabled

    monkeypatch.setattr(bypass, "_enable_window", fake_enable_window)

    result = bypass.run_with_uia_bypass(4242, lambda: "done")

    assert result == "done"
    assert calls == [(4242, False), (4242, True)]


def test_run_with_uia_bypass_restores_even_when_action_raises(monkeypatch):
    calls = []

    def fake_enable_window(hwnd, enable):
        calls.append(enable)
        return 1

    monkeypatch.setattr(bypass, "_enable_window", fake_enable_window)

    def boom():
        raise RuntimeError("pattern blew up")

    with pytest.raises(RuntimeError):
        bypass.run_with_uia_bypass(99, boom)

    assert calls == [False, True]  # finally still restored


def test_run_with_uia_bypass_restores_prior_disabled_state(monkeypatch):
    # Host was already disabled (prev=0): restore to disabled, not forced True.
    calls = []

    def fake_enable_window(hwnd, enable):
        calls.append(enable)
        return 0  # previous state: was disabled

    monkeypatch.setattr(bypass, "_enable_window", fake_enable_window)

    bypass.run_with_uia_bypass(7, lambda: None)

    assert calls == [False, False]
