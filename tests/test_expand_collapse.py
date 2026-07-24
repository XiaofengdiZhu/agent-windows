"""Unit tests for expand_element / collapse_element (TODO #12).

These cover the core layer's pattern-based expand/collapse: re-acquiring a
live UIA Control for a cached @eN selector, driving its ExpandCollapsePattern,
and the idempotency / error-classification rules.

D3 (Task 3) changed the entry point: expand/collapse now go through
``_resolve_live_control``, which resolves the live Control via FindFirst by
RuntimeId (identity lookup) instead of ControlFromPoint (visual z-order). The
FindFirst / ControlFromHandle / typed-SAFEARRAY wiring is unit-tested in
test_tree.py (test_resolve_live_control_*); here we patch
``agent_windows.core.tree._resolve_live_control`` to return a fake Control
directly, exercising only the expand/collapse state + error-classification
logic. The not-reachable / dead-element paths patch _resolve_live_control's
side_effect (typed errors bubble from the lookup, not from COM here).
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


def _patch_resolve_live(control):
    """D3: expand/collapse resolves the live Control via _resolve_live_control
    (FindFirst by RuntimeId). Patch it to return ``control`` directly — the
    FindFirst/ControlFromHandle wiring is unit-tested in test_tree.py."""
    return patch("agent_windows.core.tree._resolve_live_control",
                 return_value=control)


# ── expand_element ─────────────────────────────────────────────────

class TestExpandElement:
    def test_expand_collapsed_element(self):
        pat = _fake_pattern(int(ExpandCollapseState.Collapsed))
        ctrl = _fake_control(pat)
        with _patch_resolve_live(ctrl):
            result = expand_element("@e1")
        assert result["action"] == "expanded"
        assert result["state"] == "expanded"
        assert result["skipped"] is False
        pat.Expand.assert_called_once()

    def test_expand_already_expanded_is_idempotent_skip(self):
        pat = _fake_pattern(int(ExpandCollapseState.Expanded))
        ctrl = _fake_control(pat)
        with _patch_resolve_live(ctrl):
            result = expand_element("@e1")
        assert result["action"] == "skipped"
        assert result["skipped"] is True
        pat.Expand.assert_not_called()

    def test_expand_partially_expanded_calls_expand(self):
        # partially expanded → the agent likely wants remaining children
        # expanded, so Expand() is called (not hard-skipped). (TODO #12)
        pat = _fake_pattern(int(ExpandCollapseState.PartiallyExpanded))
        ctrl = _fake_control(pat)
        with _patch_resolve_live(ctrl):
            result = expand_element("@e1")
        assert result["action"] == "expanded"
        assert result["skipped"] is False
        pat.Expand.assert_called_once()

    def test_expand_leaf_is_skipped(self):
        # LeafNode has nothing to expand — pre-check skips without the wasted
        # ~0.5s pattern sleep. Symmetric with collapse's leaf handling.
        pat = _fake_pattern(int(ExpandCollapseState.LeafNode))
        ctrl = _fake_control(pat)
        with _patch_resolve_live(ctrl):
            result = expand_element("@e1")
        assert result["action"] == "skipped"
        assert result["skipped"] is True
        pat.Expand.assert_not_called()

    def test_expand_element_not_reachable_raises_valueerror(self):
        # _resolve_live_control raises ValueError when FindFirst misses (or the
        # coordinate fallback hits nothing) → element moved/obscured/closed.
        with patch("agent_windows.core.tree._resolve_live_control",
                   side_effect=ValueError(
                       "Element @e1 not reachable — Re-snapshot and retry.")):
            with pytest.raises(ValueError, match="not reachable"):
                expand_element("@e1")

    def test_expand_element_no_pattern_raises_valueerror(self):
        ctrl = _fake_control(None)  # GetPattern returns None
        with _patch_resolve_live(ctrl):
            with pytest.raises(ValueError, match="does not support"):
                expand_element("@e1")

    def test_expand_propagates_bad_selector(self):
        # resolve_element raises ValueError for a malformed selector (parsed
        # before any RuntimeId/coordinate work); expand_element must let it
        # propagate (→ INVALID_PARAMS at router).
        with patch("agent_windows.core.tree.resolve_element",
                   side_effect=ValueError("Invalid selector")):
            with pytest.raises(ValueError, match="Invalid selector"):
                expand_element("not-a-selector")

    def test_expand_dead_element_surfaces_typed_uia_error(self):
        """TODO #6 decision 2: _resolve_live_control wraps the only unwrapped
        live-COM call on this path. If the target's process died / disconnected
        during the reverse-lookup, the bare COMError must surface as a typed
        UIADeadElementError (→ ELEMENT_NOT_FOUND at the router) rather than
        leaking as DAEMON_INTERNAL_ERROR. The wrapper lives in
        _resolve_live_control, shared by expand/collapse, so expand covers it."""
        from agent_windows.uia.exceptions import UIADeadElementError
        from agent_windows.uia.enums import UIAError

        with patch("agent_windows.core.tree._resolve_live_control",
                   side_effect=UIADeadElementError(
                       int(UIAError.UIA_E_ELEMENTNOTAVAILABLE))):
            with pytest.raises(UIADeadElementError):
                expand_element("@e1")


# ── collapse_element ───────────────────────────────────────────────

class TestCollapseElement:
    def test_collapse_expanded_element(self):
        pat = _fake_pattern(int(ExpandCollapseState.Expanded))
        ctrl = _fake_control(pat)
        with _patch_resolve_live(ctrl):
            result = collapse_element("@e1")
        assert result["action"] == "collapsed"
        assert result["state"] == "collapsed"
        assert result["skipped"] is False
        pat.Collapse.assert_called_once()

    def test_collapse_already_collapsed_is_idempotent_skip(self):
        pat = _fake_pattern(int(ExpandCollapseState.Collapsed))
        ctrl = _fake_control(pat)
        with _patch_resolve_live(ctrl):
            result = collapse_element("@e1")
        assert result["action"] == "skipped"
        assert result["skipped"] is True
        pat.Collapse.assert_not_called()

    def test_collapse_leaf_is_skipped(self):
        # LeafNode has nothing to collapse — skip, don't error.
        pat = _fake_pattern(int(ExpandCollapseState.LeafNode))
        ctrl = _fake_control(pat)
        with _patch_resolve_live(ctrl):
            result = collapse_element("@e1")
        assert result["skipped"] is True
        pat.Collapse.assert_not_called()

    def test_collapse_element_no_pattern_raises_valueerror(self):
        ctrl = _fake_control(None)
        with _patch_resolve_live(ctrl):
            with pytest.raises(ValueError, match="does not support"):
                collapse_element("@e1")
