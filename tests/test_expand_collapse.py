"""Unit tests for expand_element / collapse_element (TODO #12).

These cover the core layer's pattern-based expand/collapse: re-acquiring a
live UIA Control from a cached @eN selector's bbox center, driving its
ExpandCollapsePattern, and the idempotency / error-classification rules.

The UIA surface is mocked — no real desktop, no COM. We patch:
- ``resolve_element_center`` to return a fixed (x, y) without touching the
  snapshot/config machinery, and
- ``ControlFromPoint`` (imported locally inside _resolve_live_control) to
  return a fake Control whose GetPattern returns a fake ExpandCollapsePattern.
"""
from unittest.mock import patch, MagicMock

import pytest

from agent_windows.core import tree
from agent_windows.core.tree import expand_element, collapse_element
from agent_windows.uia.enums import ExpandCollapseState


@pytest.fixture(autouse=True)
def _bypass_action_foreground(monkeypatch):
    """run_pattern_background fires the action foreground (no EnableWindow /
    ctypes). The EnableWindow wrapping itself is unit-tested in
    test_uia_bypass.py (test_run_pattern_background_*); here we exercise the
    expand/collapse control-acquisition + state logic without touching Win32.
    Idempotent-skip / no-pattern / not-reachable / dead-element paths return
    or raise before run_pattern_background, so this is a no-op for them."""
    monkeypatch.setattr("agent_windows.core.uia_bypass.run_pattern_background",
                        lambda control, action: action())


def _fake_pattern(state: int, *, expand_ok: bool = True, collapse_ok: bool = True):
    """A fake ExpandCollapsePattern. ``state`` is the ExpandCollapseState int
    the pattern reports; expand_ok/collapse_ok drive the bool return of the
    action (and whether the state flips)."""
    pat = MagicMock()
    pat.ExpandCollapseState = state
    # After a successful Expand/Collapse, flip the reported state so the
    # post-action read reflects the new state.
    _state = {"v": state}

    def do_expand():
        if expand_ok:
            _state["v"] = int(ExpandCollapseState.Expanded)
        return expand_ok

    def do_collapse():
        if collapse_ok:
            _state["v"] = int(ExpandCollapseState.Collapsed)
        return collapse_ok

    type(pat).ExpandCollapseState = property(lambda self: _state["v"])
    pat.Expand.side_effect = do_expand
    pat.Collapse.side_effect = do_collapse
    return pat


def _fake_control(pattern):
    """A fake Control whose GetPattern returns ``pattern`` (or None)."""
    control = MagicMock()
    control.GetPattern.return_value = pattern
    return control


def _patch_locator(x=100, y=200):
    """Patch resolve_element_center to return a fixed point (avoids the
    snapshot/config machinery)."""
    return patch("agent_windows.core.tree.resolve_element_center",
                 return_value=(x, y))


def _patch_control_from_point(control):
    """ControlFromPoint is imported locally inside _resolve_live_control, so
    patch it where it's defined."""
    return patch("agent_windows.uia.controls.ControlFromPoint",
                 return_value=control)


# ── expand_element ─────────────────────────────────────────────────

class TestExpandElement:
    def test_expand_collapsed_element(self):
        pat = _fake_pattern(int(ExpandCollapseState.Collapsed))
        ctrl = _fake_control(pat)
        with _patch_locator(), _patch_control_from_point(ctrl):
            result = expand_element("@e1")
        assert result["action"] == "expanded"
        assert result["state"] == "expanded"
        assert result["skipped"] is False
        pat.Expand.assert_called_once()

    def test_expand_already_expanded_is_idempotent_skip(self):
        pat = _fake_pattern(int(ExpandCollapseState.Expanded))
        ctrl = _fake_control(pat)
        with _patch_locator(), _patch_control_from_point(ctrl):
            result = expand_element("@e1")
        assert result["action"] == "skipped"
        assert result["skipped"] is True
        pat.Expand.assert_not_called()

    def test_expand_partially_expanded_calls_expand(self):
        # partially expanded → the agent likely wants remaining children
        # expanded, so Expand() is called (not hard-skipped). (TODO #12)
        pat = _fake_pattern(int(ExpandCollapseState.PartiallyExpanded))
        ctrl = _fake_control(pat)
        with _patch_locator(), _patch_control_from_point(ctrl):
            result = expand_element("@e1")
        assert result["action"] == "expanded"
        assert result["skipped"] is False
        pat.Expand.assert_called_once()

    def test_expand_leaf_is_skipped(self):
        # LeafNode has nothing to expand — pre-check skips without the wasted
        # ~0.5s pattern sleep. Symmetric with collapse's leaf handling.
        pat = _fake_pattern(int(ExpandCollapseState.LeafNode))
        ctrl = _fake_control(pat)
        with _patch_locator(), _patch_control_from_point(ctrl):
            result = expand_element("@e1")
        assert result["action"] == "skipped"
        assert result["skipped"] is True
        pat.Expand.assert_not_called()

    def test_expand_element_not_reachable_raises_valueerror(self):
        # ControlFromPoint returns None → element moved/obscured/closed.
        with _patch_locator(), _patch_control_from_point(None):
            with pytest.raises(ValueError, match="not reachable"):
                expand_element("@e1")

    def test_expand_element_no_pattern_raises_valueerror(self):
        ctrl = _fake_control(None)  # GetPattern returns None
        with _patch_locator(), _patch_control_from_point(ctrl):
            with pytest.raises(ValueError, match="does not support"):
                expand_element("@e1")

    def test_expand_propagates_bad_selector(self):
        # resolve_element_center raises ValueError for a malformed selector;
        # expand_element must let it propagate (→ INVALID_PARAMS at router).
        with patch("agent_windows.core.tree.resolve_element_center",
                   side_effect=ValueError("Invalid selector")):
            with pytest.raises(ValueError, match="Invalid selector"):
                expand_element("not-a-selector")

    def test_expand_dead_element_surfaces_typed_uia_error(self):
        """TODO #6 decision 2: ControlFromPoint is the only unwrapped live-COM
        call on this path. If the target's process died / disconnected during
        the reverse-lookup, the bare COMError must surface as a typed
        UIADeadElementError (→ ELEMENT_NOT_FOUND at the router) rather than
        leaking as DAEMON_INTERNAL_ERROR. The wrapper lives in
        _resolve_live_control, shared by expand/collapse, so expand covers it."""
        from _ctypes import COMError
        from agent_windows.uia.exceptions import UIADeadElementError
        from agent_windows.uia.enums import UIAError

        dead = COMError(
            int(UIAError.UIA_E_ELEMENTNOTAVAILABLE), "element gone", (None,))
        with _patch_locator(), \
                patch("agent_windows.uia.controls.ControlFromPoint",
                      side_effect=dead):
            with pytest.raises(UIADeadElementError):
                expand_element("@e1")


# ── collapse_element ───────────────────────────────────────────────

class TestCollapseElement:
    def test_collapse_expanded_element(self):
        pat = _fake_pattern(int(ExpandCollapseState.Expanded))
        ctrl = _fake_control(pat)
        with _patch_locator(), _patch_control_from_point(ctrl):
            result = collapse_element("@e1")
        assert result["action"] == "collapsed"
        assert result["state"] == "collapsed"
        assert result["skipped"] is False
        pat.Collapse.assert_called_once()

    def test_collapse_already_collapsed_is_idempotent_skip(self):
        pat = _fake_pattern(int(ExpandCollapseState.Collapsed))
        ctrl = _fake_control(pat)
        with _patch_locator(), _patch_control_from_point(ctrl):
            result = collapse_element("@e1")
        assert result["action"] == "skipped"
        assert result["skipped"] is True
        pat.Collapse.assert_not_called()

    def test_collapse_leaf_is_skipped(self):
        # LeafNode has nothing to collapse — skip, don't error.
        pat = _fake_pattern(int(ExpandCollapseState.LeafNode))
        ctrl = _fake_control(pat)
        with _patch_locator(), _patch_control_from_point(ctrl):
            result = collapse_element("@e1")
        assert result["skipped"] is True
        pat.Collapse.assert_not_called()

    def test_collapse_element_no_pattern_raises_valueerror(self):
        ctrl = _fake_control(None)
        with _patch_locator(), _patch_control_from_point(ctrl):
            with pytest.raises(ValueError, match="does not support"):
                collapse_element("@e1")
