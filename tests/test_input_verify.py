"""D3 Task 4: verify selector path reads the FindFirst-resolved control.

``_append_hit_focus`` / ``_append_drop_slider`` power the post-op ``Hit:`` /
``Drop:`` / ``Slider:`` verify lines. Pre-D3 they always read the element under
the click coord via ``hit_element_at(x, y)`` (= ``ControlFromPoint`` = visual
z-order topmost), so a click that targeted an occluded element via a selector
(``@eN``) reported the *wrong* post-op element. D3 Task 4 routes the selector
path through ``hit_element_of(control)`` (the FindFirst-resolved element) while
leaving the coordinate path (``--at x,y``) untouched.

These tests pin the three behaviors:
  1. control passed (selector path) → ``hit_element_of`` is used, NOT
     ``hit_element_at``;
  2. no control (coordinate path) → ``hit_element_at`` unchanged;
  3. ``hit_element_of`` raises/returns None → degrade to ``hit_element_at``
     (verify never raises, never blocks the op).

The handler-level integration tests (TestClickVerifyControlSelectorPath /
TestTypeVerifyControlAtSelectorPath) pin that the selector path actually
resolves the live control via ``_resolve_live_control`` and threads it through.
"""
from unittest.mock import patch

import pytest

from agent_windows.daemon.handlers import input as input_handlers


# ── _append_hit_focus: control routing ─────────────────────────────────


class TestAppendHitFocusControlRouting:
    """The three D3 Task 4 cases for the ``Hit:`` line on click/hover/move."""

    def test_with_control_uses_hit_element_of(self, monkeypatch):
        """When a control is passed (selector path), Hit reads it via
        ``hit_element_of`` — NOT ``hit_element_at(x, y)`` (which would resolve
        by visual z-order and pick up the occluder)."""
        calls = {"at": 0, "of": 0}

        def _hit_at(x, y):
            calls["at"] += 1
            return {"control_type": "WRONG", "name": "visual-topmost"}

        def _hit_of(c):
            calls["of"] += 1
            return {"control_type": "Button", "name": "intended"}

        monkeypatch.setattr(input_handlers, "_verify_enabled", lambda: True)
        monkeypatch.setattr("agent_windows.core.tree.hit_element_at", _hit_at)
        monkeypatch.setattr("agent_windows.core.tree.hit_element_of", _hit_of)
        monkeypatch.setattr("agent_windows.core.tree.foreground_window",
                            lambda: {"title": ""})

        lines = []
        input_handlers._append_hit_focus(lines, 5, 6, control="LIVE_CTRL")

        assert calls["at"] == 0, "selector path must NOT read ElementFromPoint"
        assert calls["of"] == 1, "selector path must read the FindFirst control"
        assert 'Hit: Button "intended"' in lines[0]

    def test_without_control_uses_hit_element_at(self, monkeypatch):
        """No control (coordinate path, ``--at x,y``) → ``hit_element_at(x, y)``
        unchanged. Boundary: no selector, no element identity — the coord IS the
        only signal, so z-order topmost at the point remains correct."""
        monkeypatch.setattr(input_handlers, "_verify_enabled", lambda: True)
        monkeypatch.setattr("agent_windows.core.tree.hit_element_at",
                            lambda x, y: {"control_type": "Edit", "name": "coord"})
        monkeypatch.setattr("agent_windows.core.tree.foreground_window",
                            lambda: {"title": ""})

        lines = []
        input_handlers._append_hit_focus(lines, 5, 6)  # no control

        assert 'Hit: Edit "coord"' in lines[0]

    def test_control_returns_none_degrades_to_hit_element_at(self, monkeypatch):
        """``hit_element_of`` returning None (control read failed silently) →
        fall back to ``hit_element_at`` so the Hit: line still surfaces something
        useful. Best-effort: never raise, never silently no-op."""
        monkeypatch.setattr(input_handlers, "_verify_enabled", lambda: True)
        monkeypatch.setattr("agent_windows.core.tree.hit_element_of",
                            lambda c: None)
        monkeypatch.setattr("agent_windows.core.tree.hit_element_at",
                            lambda x, y: {"control_type": "Edit", "name": "fallback"})
        monkeypatch.setattr("agent_windows.core.tree.foreground_window",
                            lambda: {"title": ""})

        lines = []
        input_handlers._append_hit_focus(lines, 5, 6, control="CTRL")

        assert 'Hit: Edit "fallback"' in lines[0]

    def test_control_read_raises_degrades_to_hit_element_at(self, monkeypatch):
        """If ``hit_element_of`` raises (FindFirst read blew up mid-call), degrade
        to ``hit_element_at`` — verify's best-effort contract forbids raising."""
        monkeypatch.setattr(input_handlers, "_verify_enabled", lambda: True)

        def _of_boom(c):
            raise RuntimeError("control read blew up")

        monkeypatch.setattr("agent_windows.core.tree.hit_element_of", _of_boom)
        monkeypatch.setattr("agent_windows.core.tree.hit_element_at",
                            lambda x, y: {"control_type": "Edit", "name": "fallback"})
        monkeypatch.setattr("agent_windows.core.tree.foreground_window",
                            lambda: {"title": ""})

        lines = []
        input_handlers._append_hit_focus(lines, 5, 6, control="CTRL")

        assert 'Hit: Edit "fallback"' in lines[0]


# ── _append_drop_slider: control routing (drag Drop: line) ────────────


class TestAppendDropSliderControlRouting:
    """Mirror of TestAppendHitFocusControlRouting for drag's Drop: line."""

    def test_with_control_uses_hit_element_of(self, monkeypatch):
        calls = {"at": 0, "of": 0}

        def _hit_at(x, y):
            calls["at"] += 1
            return {"control_type": "WRONG", "name": "visual-topmost"}

        def _hit_of(c):
            calls["of"] += 1
            return {"control_type": "ListItem", "name": "intended-drop"}

        monkeypatch.setattr(input_handlers, "_verify_enabled", lambda: True)
        monkeypatch.setattr("agent_windows.core.tree.hit_element_at", _hit_at)
        monkeypatch.setattr("agent_windows.core.tree.hit_element_of", _hit_of)
        monkeypatch.setattr("agent_windows.core.tree.range_percent_at",
                            lambda x, y: None)

        lines = []
        input_handlers._append_drop_slider(lines, 5, 6, control="LIVE_CTRL")

        assert calls["at"] == 0
        assert calls["of"] == 1
        assert 'Drop: ListItem "intended-drop"' in lines[0]

    def test_without_control_uses_hit_element_at(self, monkeypatch):
        monkeypatch.setattr(input_handlers, "_verify_enabled", lambda: True)
        monkeypatch.setattr("agent_windows.core.tree.hit_element_at",
                            lambda x, y: {"control_type": "List", "name": "coord"})
        monkeypatch.setattr("agent_windows.core.tree.range_percent_at",
                            lambda x, y: None)

        lines = []
        input_handlers._append_drop_slider(lines, 5, 6)

        assert 'Drop: List "coord"' in lines[0]

    def test_control_read_raises_degrades_to_hit_element_at(self, monkeypatch):
        monkeypatch.setattr(input_handlers, "_verify_enabled", lambda: True)

        def _of_boom(c):
            raise RuntimeError("control read blew up")

        monkeypatch.setattr("agent_windows.core.tree.hit_element_of", _of_boom)
        monkeypatch.setattr("agent_windows.core.tree.hit_element_at",
                            lambda x, y: {"control_type": "List", "name": "fallback"})
        monkeypatch.setattr("agent_windows.core.tree.range_percent_at",
                            lambda x, y: None)

        lines = []
        input_handlers._append_drop_slider(lines, 5, 6, control="CTRL")

        assert 'Drop: List "fallback"' in lines[0]


# ── click handler: selector path threads verify_control through ───────


class TestClickVerifyControlSelectorPath:
    """The click handler's non-background selector path resolves the live control
    via ``_resolve_live_control`` and passes it to ``_append_hit_focus`` so the
    post-op ``Hit:`` line names the intended element, not the visual topmost.

    Background clicks are excluded: they deliver via Invoke, and the existing
    background tests (test_uia_bypass) pin that path. ``_coords`` is patched so
    no live UIA tree is touched in unit tests."""

    _CENTER = (200, 300)

    def _patch_center(self):
        return patch("agent_windows.core.tree.resolve_element_center",
                     return_value=self._CENTER)

    def test_selector_path_passes_resolved_control_to_verify(self, monkeypatch):
        resolved = {"_sentinel": "LIVE_FINDFIRST_CONTROL"}

        captured = {}
        monkeypatch.setattr(input_handlers, "_verify_enabled", lambda: True)
        monkeypatch.setattr("agent_windows.core.tree._resolve_live_control",
                            lambda sel: resolved)
        monkeypatch.setattr("agent_windows.core.tree.hit_element_of",
                            lambda c: (
                                captured.update({"control": c}) or None,
                                {"control_type": "Button", "name": "intended"},
                            )[1])
        monkeypatch.setattr("agent_windows.core.tree.hit_element_at",
                            lambda x, y: {"control_type": "Edit", "name": "occluder"})
        monkeypatch.setattr("agent_windows.core.tree.foreground_window",
                            lambda: {"title": ""})

        with self._patch_center(), \
                patch("agent_windows.core.input_controller.click"):
            r = input_handlers._click({"selector": "@e3"})

        # _append_hit_focus received control=resolved → hit_element_of ran with
        # the FindFirst control, NOT hit_element_at (would have read "occluder").
        assert captured["control"] is resolved
        assert 'Hit: Button "intended"' in r["output"]
        assert "occluder" not in r["output"]

    def test_selector_path_resolve_failure_does_not_block_click(
            self, monkeypatch):
        """``_resolve_live_control`` raising (element rebuilt after snapshot —
        common after page-nav) must NOT abort the click: handler wraps it
        best-effort, falls back to ``None``, and verify degrades to
        ``hit_element_at`` (which on a successful click reports the new element
        at the coord)."""
        monkeypatch.setattr(input_handlers, "_verify_enabled", lambda: True)

        def _resolve_boom(sel):
            raise RuntimeError("FindFirst miss: element rebuilt")

        monkeypatch.setattr("agent_windows.core.tree._resolve_live_control",
                            _resolve_boom)
        monkeypatch.setattr("agent_windows.core.tree.hit_element_at",
                            lambda x, y: {"control_type": "Edit", "name": "new-at-coord"})
        monkeypatch.setattr("agent_windows.core.tree.foreground_window",
                            lambda: {"title": ""})

        with self._patch_center(), \
                patch("agent_windows.core.input_controller.click") as m_click:
            r = input_handlers._click({"selector": "@e3"})

        # Click still fired at the resolved center — verify hiccup didn't block.
        m_click.assert_called_once_with(200, 300, right=False, double=False)
        assert "Position: (200, 300)" in r["output"]
        # verify degraded to hit_element_at (no FindFirst control available).
        assert 'Hit: Edit "new-at-coord"' in r["output"]

    def test_coord_path_does_not_resolve_live_control(self, monkeypatch):
        """No selector (``--at x,y``) → ``_resolve_live_control`` is NOT called
        (there's no element identity to look up); verify reads the coord via
        ``hit_element_at`` unchanged (D3 doesn't touch the coordinate path)."""
        called = {"resolve": False}

        def _resolve(sel):
            called["resolve"] = True
            return None

        monkeypatch.setattr(input_handlers, "_verify_enabled", lambda: True)
        monkeypatch.setattr("agent_windows.core.tree._resolve_live_control",
                            _resolve)
        monkeypatch.setattr("agent_windows.core.tree.hit_element_at",
                            lambda x, y: {"control_type": "Edit", "name": "coord"})
        monkeypatch.setattr("agent_windows.core.tree.foreground_window",
                            lambda: {"title": ""})

        with patch("agent_windows.core.input_controller.click"):
            input_handlers._click({"x": 10, "y": 20})

        assert called["resolve"] is False

    def test_background_selector_path_does_not_resolve_verify_control(
            self, monkeypatch):
        """Background + selector uses Invoke delivery (handled in
        ``invoke_background``); the handler must NOT additionally resolve a
        verify control for the background branch. ``_verify_enabled`` is left on
        so we can see verify still ran on the coord path (best-effort echo).
        ``_coords`` is stubbed (echo-only on the background selector branch) so
        no live UIA tree is touched."""
        resolved = {"_sentinel": "should_not_be_used_for_verify"}

        captured = {"resolve_calls": 0}
        monkeypatch.setattr(input_handlers, "_verify_enabled", lambda: True)
        # Background + selector: _coords is echo-only and best-effort; stub it
        # so resolve_element_center / live UIA never runs in the unit test.
        monkeypatch.setattr(input_handlers, "_coords",
                            lambda p: (p.get("x", 0), p.get("y", 0)))
        monkeypatch.setattr("agent_windows.core.tree._resolve_live_control",
                            lambda sel: captured.__setitem__(
                                "resolve_calls", captured["resolve_calls"] + 1) or resolved)
        monkeypatch.setattr("agent_windows.core.uia_bypass.invoke_background",
                            lambda x, y, selector: {"action": "invoke"})
        monkeypatch.setattr("agent_windows.core.tree.hit_element_at",
                            lambda x, y: {"control_type": "Edit", "name": "coord"})
        monkeypatch.setattr("agent_windows.core.tree.foreground_window",
                            lambda: {"title": ""})

        r = input_handlers._click({"selector": "@e3", "background": True,
                                   "x": 5, "y": 6})

        # No handler-level resolve happened (background excluded per brief).
        assert captured["resolve_calls"] == 0
        # Verify still ran on the coord path (best-effort echo).
        assert 'Hit: Edit "coord"' in r["output"]


# ── type handler: at_selector path threads verify_control through ─────


class TestTypeVerifyControlAtSelectorPath:
    """The type handler's ``--at-selector`` path (click-then-type) likewise
    threads the FindFirst-resolved control through ``_append_hit_focus``."""

    _CENTER = (200, 300)

    def _patch_center(self):
        return patch("agent_windows.core.tree.resolve_element_center",
                     return_value=self._CENTER)

    def test_at_selector_passes_resolved_control_to_verify(self, monkeypatch):
        resolved = {"_sentinel": "LIVE_FINDFIRST_CONTROL"}
        captured = {}

        monkeypatch.setattr(input_handlers, "_verify_enabled", lambda: True)
        monkeypatch.setattr("agent_windows.core.tree._resolve_live_control",
                            lambda sel: resolved)
        monkeypatch.setattr("agent_windows.core.tree.hit_element_of",
                            lambda c: (
                                captured.update({"control": c}) or None,
                                {"control_type": "Edit", "name": "intended"},
                            )[1])
        monkeypatch.setattr("agent_windows.core.tree.hit_element_at",
                            lambda x, y: {"control_type": "Edit", "name": "occluder"})
        monkeypatch.setattr("agent_windows.core.tree.field_value_at",
                            lambda x, y: None)
        monkeypatch.setattr("agent_windows.core.tree.foreground_window",
                            lambda: {"title": ""})

        with self._patch_center(), \
                patch("agent_windows.core.input_controller.type_text_at"):
            r = input_handlers._type({"text": "hi", "at_selector": "@e3"})

        assert captured["control"] is resolved
        assert 'Hit: Edit "intended"' in r["output"]
        assert "occluder" not in r["output"]
