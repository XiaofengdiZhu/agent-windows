"""Tests for daemon tree handlers (snapshot --window/--active scoping)."""
import pytest
from unittest.mock import patch
from agent_windows.daemon.router import HANDLERS
from agent_windows.core.tree import WindowInfo


def _win(title="App", process="app.exe", handle=1, active=False):
    return WindowInfo(
        title=title, process_name=process, handle=handle,
        bbox=None, is_active=active, class_name="", status="",
        is_browser=False,
    )


class TestSnapshotWindowScope:
    @patch("agent_windows.utils.config.load_config")
    @patch("agent_windows.core.tree.render_snapshot")
    @patch("agent_windows.core.tree.get_tree_snapshot")
    @patch("agent_windows.core.tree.resolve_window")
    def test_window_forwards_to_resolve_and_snapshot(
            self, mock_resolve, mock_gts, mock_render, mock_cfg):
        win = _win(title="Notepad", process="notepad.exe", handle=12345)
        mock_resolve.return_value = (win, None)
        mock_gts.return_value = {"semantic_tree": "", "interactive_elements": []}
        mock_render.return_value = "RENDERED"
        mock_cfg.return_value = {}

        out = HANDLERS["snapshot"]({"window": "note"})

        mock_resolve.assert_called_once_with(value="note", active=False)
        mock_gts.assert_called_once()
        _, kwargs = mock_gts.call_args
        assert kwargs.get("only_window") is win
        assert "## Window:" in out["output"]
        assert "Notepad" in out["output"]
        assert "notepad.exe" in out["output"]
        assert "RENDERED" in out["output"]

    @patch("agent_windows.utils.config.load_config")
    @patch("agent_windows.core.tree.render_snapshot")
    @patch("agent_windows.core.tree.get_tree_snapshot")
    @patch("agent_windows.core.tree.resolve_window")
    def test_active_forwards(self, mock_resolve, mock_gts, mock_render, mock_cfg):
        win = _win(title="FG", process="fg.exe", handle=1, active=True)
        mock_resolve.return_value = (win, None)
        mock_gts.return_value = {"semantic_tree": "", "interactive_elements": []}
        mock_render.return_value = "R"
        mock_cfg.return_value = {}

        HANDLERS["snapshot"]({"active": True})

        mock_resolve.assert_called_once_with(value=None, active=True)

    @patch("agent_windows.utils.config.load_config")
    @patch("agent_windows.core.tree.render_snapshot")
    @patch("agent_windows.core.tree.get_tree_snapshot")
    @patch("agent_windows.core.tree.resolve_window")
    def test_warning_prepended_before_body(
            self, mock_resolve, mock_gts, mock_render, mock_cfg):
        win = _win(title="calc — main", process="calc.exe", handle=1)
        mock_resolve.return_value = (win, '⚠ Matched 2 windows by title "calc"')
        mock_gts.return_value = {"semantic_tree": "", "interactive_elements": []}
        mock_render.return_value = "BODY"
        mock_cfg.return_value = {}

        out = HANDLERS["snapshot"]({"window": "calc"})

        assert "## Window:" in out["output"]
        assert '⚠ Matched 2 windows' in out["output"]
        assert out["output"].index("## Window:") < out["output"].index("BODY")

    @patch("agent_windows.utils.config.load_config")
    @patch("agent_windows.core.tree.render_snapshot")
    @patch("agent_windows.core.tree.get_tree_snapshot")
    def test_no_window_no_header(self, mock_gts, mock_render, mock_cfg):
        mock_gts.return_value = {"semantic_tree": "", "interactive_elements": []}
        mock_render.return_value = "PLAIN"
        mock_cfg.return_value = {}

        out = HANDLERS["snapshot"]({})

        assert "## Window:" not in out["output"]
        _, kwargs = mock_gts.call_args
        assert kwargs.get("only_window") is None

    @patch("agent_windows.utils.config.load_config")
    @patch("agent_windows.core.tree.render_snapshot")
    @patch("agent_windows.core.tree.get_tree_snapshot")
    def test_interactive_only_forwarded_to_render(self, mock_gts, mock_render, mock_cfg):
        """interactive_only=True is passed through to render_snapshot."""
        mock_gts.return_value = {"semantic_tree": "", "interactive_elements": []}
        mock_render.return_value = "SLIM"
        mock_cfg.return_value = {}

        HANDLERS["snapshot"]({"interactive_only": True})

        _, kwargs = mock_render.call_args
        assert kwargs.get("interactive_only") is True

    @patch("agent_windows.utils.config.load_config")
    @patch("agent_windows.core.tree.render_snapshot")
    @patch("agent_windows.core.tree.get_tree_snapshot")
    def test_interactive_only_string_true_coerced(self, mock_gts, mock_render, mock_cfg):
        """IPC may stringify the bool; 'true' (any case) coerces to True."""
        mock_gts.return_value = {"semantic_tree": "", "interactive_elements": []}
        mock_render.return_value = "SLIM"
        mock_cfg.return_value = {}

        HANDLERS["snapshot"]({"interactive_only": "true"})

        _, kwargs = mock_render.call_args
        assert kwargs.get("interactive_only") is True

    @patch("agent_windows.utils.config.load_config")
    @patch("agent_windows.core.tree.render_snapshot")
    @patch("agent_windows.core.tree.get_tree_snapshot")
    def test_interactive_only_absent_defaults_false(self, mock_gts, mock_render, mock_cfg):
        """No interactive_only param → render_snapshot gets interactive_only=False."""
        mock_gts.return_value = {"semantic_tree": "", "interactive_elements": []}
        mock_render.return_value = "FULL"
        mock_cfg.return_value = {}

        HANDLERS["snapshot"]({})

        _, kwargs = mock_render.call_args
        assert kwargs.get("interactive_only") is False

    def test_window_and_active_mutually_exclusive(self):
        with pytest.raises(ValueError, match="mutually exclusive"):
            HANDLERS["snapshot"]({"window": "x", "active": True})

    @patch("agent_windows.core.tree.resolve_window")
    def test_resolve_not_found_propagates_as_value_error(self, mock_resolve):
        mock_resolve.side_effect = ValueError("Window not found: zzz")
        with pytest.raises(ValueError, match="Window not found"):
            HANDLERS["snapshot"]({"window": "zzz"})

    def test_snapshot_window_writes_tree_cache_for_en_alignment(self):
        """C1 regression: snapshot --window must route through get_tree_snapshot
        so the scoped subtree is written to ``_tree_cache``. ``resolve_element(
        '@eN')`` reads that same slot — an earlier impl called ``traverse_tree``
        directly, leaving the subtree uncached so @e1 resolved to the full-
        desktop first element (``click @e1`` could hit the wrong target)."""
        import agent_windows.core.tree as tm
        from agent_windows.core.tree import BoundingBox

        target = WindowInfo(
            title="Target", process_name="t.exe", handle=99,
            bbox=BoundingBox(0, 0, 10, 10), is_active=False,
            class_name="", status="", is_browser=False)
        # Reset the module-level cache so prior tests don't satisfy the assert.
        tm._tree_cache = {}
        tm._tree_cache_ts = 0.0

        patches = [
            patch("agent_windows.core.tree.enumerate_windows", return_value=[target]),
            patch("agent_windows.core.tree._traverse_window"),
            patch("agent_windows.core.tree._build_traversal_cache", return_value=object()),
            patch("agent_windows.uia.core._AutomationClient"),
            patch("agent_windows.core.screenshot.get_dpi_scale", return_value=1.0),
            patch("agent_windows.core.screenshot.get_virtual_screen_rect"),
            patch("agent_windows.core.tree.render_snapshot", return_value="R"),
            patch("agent_windows.utils.config.load_config", return_value={}),
        ]
        for p in patches:
            p.start()
        try:
            HANDLERS["snapshot"]({"window": "target"})
        finally:
            for p in patches:
                p.stop()

        assert tm._tree_cache, (
            "snapshot --window must populate _tree_cache via get_tree_snapshot "
            "so resolve_element('@eN') aligns to the subtree, not the full desktop")
        assert tm._tree_cache["windows"] == [target]


# ── snapshot-diff handler ────────────────────────────────────────────


def _fake_snapshot_data(els=None, wins=None):
    """Minimal traverse_tree-shaped dict for diff_snapshots/render_snapshot_diff."""
    return {"interactive_elements": els or [], "windows": wins or [],
            "active_window": None, "semantic_tree": ""}


def _patch_diff_common(monkeypatch):
    """Patch load_config (deterministic, no file I/O) for snapshot-diff tests."""
    monkeypatch.setattr("agent_windows.utils.config.load_config", lambda: {})


def test_snapshot_diff_save_stores_slot_and_confirms(monkeypatch):
    import agent_windows.daemon.handlers.tree as h
    from agent_windows.core.tree import ElementInfo, BoundingBox
    monkeypatch.setattr(h, "_snapshot_diff_baseline", None, raising=False)
    _patch_diff_common(monkeypatch)
    monkeypatch.setattr("agent_windows.core.tree.traverse_tree",
                        lambda **k: _fake_snapshot_data(
                            [ElementInfo("A", "ButtonControl", BoundingBox(0, 0, 1, 1))]))
    monkeypatch.setattr("agent_windows.core.tree.resolve_window",
                        lambda value=None, active=False: (None, ""))
    out = HANDLERS["snapshot_diff"]({"action": "save"})
    assert "baseline saved: 1 elements" in out["output"]
    assert h._snapshot_diff_baseline is not None
    assert len(h._snapshot_diff_baseline["interactive_elements"]) == 1


def test_snapshot_diff_diff_empty_slot_is_invalid_params(monkeypatch):
    import agent_windows.daemon.handlers.tree as h
    monkeypatch.setattr(h, "_snapshot_diff_baseline", None, raising=False)
    _patch_diff_common(monkeypatch)
    with pytest.raises(ValueError, match="baseline"):
        HANDLERS["snapshot_diff"]({"action": "diff"})


def test_snapshot_diff_save_then_diff_then_clear(monkeypatch):
    import agent_windows.daemon.handlers.tree as h
    from agent_windows.core.tree import ElementInfo, BoundingBox
    seq = iter([
        _fake_snapshot_data([ElementInfo("A", "ButtonControl", BoundingBox(0, 0, 1, 1))]),
        _fake_snapshot_data([ElementInfo("A", "ButtonControl", BoundingBox(0, 0, 1, 1)),
                             ElementInfo("B", "ButtonControl", BoundingBox(500, 500, 501, 501))]),
    ])
    monkeypatch.setattr(h, "_snapshot_diff_baseline", None, raising=False)
    _patch_diff_common(monkeypatch)
    monkeypatch.setattr("agent_windows.core.tree.traverse_tree", lambda **k: next(seq))
    monkeypatch.setattr("agent_windows.core.tree.resolve_window",
                        lambda value=None, active=False: (None, ""))
    monkeypatch.setattr("agent_windows.core.tree.prime_tree_cache", lambda d: None)
    HANDLERS["snapshot_diff"]({"action": "save"})
    out = HANDLERS["snapshot_diff"]({"action": "diff"})
    assert "Added" in out["output"]
    clr = HANDLERS["snapshot_diff"]({"action": "clear"})
    assert "cleared" in clr["output"]
    assert h._snapshot_diff_baseline is None


def test_snapshot_diff_bypasses_2s_cache(monkeypatch):
    import agent_windows.daemon.handlers.tree as h
    calls = {"traverse": 0, "cached": 0}
    monkeypatch.setattr(h, "_snapshot_diff_baseline", None, raising=False)
    _patch_diff_common(monkeypatch)
    monkeypatch.setattr("agent_windows.core.tree.traverse_tree",
                        lambda **k: (calls.__setitem__("traverse", calls["traverse"] + 1)
                                     or _fake_snapshot_data()))
    monkeypatch.setattr("agent_windows.core.tree.get_tree_snapshot",
                        lambda **k: (calls.__setitem__("cached", calls["cached"] + 1)
                                     or _fake_snapshot_data()))
    monkeypatch.setattr("agent_windows.core.tree.resolve_window",
                        lambda value=None, active=False: (None, ""))
    monkeypatch.setattr("agent_windows.core.tree.prime_tree_cache", lambda d: None)
    HANDLERS["snapshot_diff"]({"action": "save"})
    HANDLERS["snapshot_diff"]({"action": "diff"})
    assert calls["traverse"] == 2 and calls["cached"] == 0


def test_snapshot_diff_reuses_scope_from_slot(monkeypatch):
    import agent_windows.daemon.handlers.tree as h
    seen = {}
    monkeypatch.setattr(h, "_snapshot_diff_baseline", None, raising=False)
    _patch_diff_common(monkeypatch)
    monkeypatch.setattr("agent_windows.core.tree.resolve_window",
                        lambda value=None, active=False: (seen.__setitem__("value", value) or (None, "")))
    monkeypatch.setattr("agent_windows.core.tree.traverse_tree",
                        lambda **k: _fake_snapshot_data())
    monkeypatch.setattr("agent_windows.core.tree.prime_tree_cache", lambda d: None)
    HANDLERS["snapshot_diff"]({"action": "save", "window": "Notepad"})
    HANDLERS["snapshot_diff"]({"action": "diff"})
    assert seen["value"] == "Notepad"


def test_snapshot_diff_primes_cache_with_current(monkeypatch):
    import agent_windows.daemon.handlers.tree as h
    primed = {}
    monkeypatch.setattr(h, "_snapshot_diff_baseline", None, raising=False)
    _patch_diff_common(monkeypatch)
    monkeypatch.setattr("agent_windows.core.tree.traverse_tree",
                        lambda **k: _fake_snapshot_data())
    monkeypatch.setattr("agent_windows.core.tree.resolve_window",
                        lambda value=None, active=False: (None, ""))
    monkeypatch.setattr("agent_windows.core.tree.prime_tree_cache",
                        lambda d: primed.__setitem__("data", d))
    HANDLERS["snapshot_diff"]({"action": "save"})
    assert "data" not in primed          # save must NOT prime
    HANDLERS["snapshot_diff"]({"action": "diff"})
    assert "data" in primed              # diff primes with current


def test_snapshot_diff_injects_scope_into_header(monkeypatch):
    import agent_windows.daemon.handlers.tree as h
    monkeypatch.setattr(h, "_snapshot_diff_baseline", None, raising=False)
    _patch_diff_common(monkeypatch)
    monkeypatch.setattr("agent_windows.core.tree.traverse_tree",
                        lambda **k: _fake_snapshot_data())
    monkeypatch.setattr("agent_windows.core.tree.resolve_window",
                        lambda value=None, active=False: (None, ""))
    monkeypatch.setattr("agent_windows.core.tree.prime_tree_cache", lambda d: None)
    HANDLERS["snapshot_diff"]({"action": "save", "window": "Notepad"})
    out = HANDLERS["snapshot_diff"]({"action": "diff"})
    assert '(scope: "Notepad")' in out["output"]


def test_snapshot_diff_display_primes_unfiltered_tree(monkeypatch):
    """Lock #1: with --display, prime_tree_cache must receive the UNFILTERED
    tree. resolve_element aligns @eN to the unfiltered snapshot (display
    survivors carry _orig_idx pointing into it); priming the filtered tree
    would make @eN refs resolve out-of-bounds / misaligned. The diff/render
    path still walks the filtered tree (scope-consistent with the baseline)."""
    import agent_windows.daemon.handlers.tree as h
    from agent_windows.core.tree import ElementInfo, BoundingBox
    unfiltered = _fake_snapshot_data([
        ElementInfo("A", "ButtonControl", BoundingBox(0, 0, 1, 1)),
        ElementInfo("B", "ButtonControl", BoundingBox(2, 2, 3, 3)),
        ElementInfo("C", "ButtonControl", BoundingBox(4, 4, 5, 5)),
    ])
    filtered = _fake_snapshot_data([
        ElementInfo("A", "ButtonControl", BoundingBox(0, 0, 1, 1)),
    ])
    primed = {}
    monkeypatch.setattr(h, "_snapshot_diff_baseline", None, raising=False)
    _patch_diff_common(monkeypatch)
    monkeypatch.setattr("agent_windows.core.tree.traverse_tree",
                        lambda **k: unfiltered)
    monkeypatch.setattr(
        "agent_windows.core.display.filter_snapshot_to_displays",
        lambda data, indices: filtered)
    monkeypatch.setattr("agent_windows.core.tree.resolve_window",
                        lambda value=None, active=False: (None, ""))
    monkeypatch.setattr("agent_windows.core.tree.prime_tree_cache",
                        lambda d: primed.__setitem__("data", d))
    # save stores the FILTERED baseline (1 el); display scope is consistent.
    HANDLERS["snapshot_diff"]({"action": "save", "display": [1]})
    assert len(h._snapshot_diff_baseline["interactive_elements"]) == 1
    HANDLERS["snapshot_diff"]({"action": "diff", "display": [1]})
    # prime must receive the UNFILTERED tree (3 els), not the filtered (1 el).
    assert len(primed["data"]["interactive_elements"]) == 3


def test_snapshot_diff_window_and_active_mutually_exclusive(monkeypatch):
    _patch_diff_common(monkeypatch)
    with pytest.raises(ValueError, match="mutually exclusive"):
        HANDLERS["snapshot_diff"]({"window": "x", "active": True})


def test_snapshot_diff_unknown_action_is_invalid_params(monkeypatch):
    _patch_diff_common(monkeypatch)
    with pytest.raises(ValueError, match="action must be"):
        HANDLERS["snapshot_diff"]({"action": "foo"})


def test_snapshot_diff_surfaces_resolve_window_warning(monkeypatch):
    """N-1: ``_capture_for_diff`` used to discard ``resolve_window``'s
    ambiguity warning. It now surfaces it on both save (appended) and diff
    (prepended) so an ambiguous --window match is visible to the agent,
    mirroring the ``snapshot`` handler's header warning."""
    import agent_windows.daemon.handlers.tree as h
    warn = '⚠ Matched 2 windows by title "calc"'
    monkeypatch.setattr(h, "_snapshot_diff_baseline", None, raising=False)
    _patch_diff_common(monkeypatch)
    monkeypatch.setattr("agent_windows.core.tree.traverse_tree",
                        lambda **k: _fake_snapshot_data())
    monkeypatch.setattr("agent_windows.core.tree.resolve_window",
                        lambda value=None, active=False: (None, warn))
    monkeypatch.setattr("agent_windows.core.tree.prime_tree_cache", lambda d: None)
    saved = HANDLERS["snapshot_diff"]({"action": "save", "window": "calc"})
    assert warn in saved["output"]
    diffed = HANDLERS["snapshot_diff"]({"action": "diff"})
    assert warn in diffed["output"]


def test_snapshot_diff_closed_stored_window_raises_value_error(monkeypatch):
    """Lock the error code for a baseline scoped to a window that no longer
    resolves on diff (closed between save and diff). ``resolve_window`` raises
    ``ValueError("Window not found")``, and ``route_request`` maps ``ValueError``
    → ``INVALID_PARAMS`` (-32602) — NOT ``ELEMENT_NOT_FOUND`` (-1001), which is
    reserved for ``FileNotFoundError``/``UIADeadElementError``. A earlier spec
    row claimed -1001; this test pins the implemented -32602 behavior."""
    import agent_windows.daemon.handlers.tree as h
    monkeypatch.setattr(h, "_snapshot_diff_baseline", None, raising=False)
    _patch_diff_common(monkeypatch)
    monkeypatch.setattr("agent_windows.core.tree.traverse_tree",
                        lambda **k: _fake_snapshot_data())
    monkeypatch.setattr("agent_windows.core.tree.prime_tree_cache", lambda d: None)
    # save resolves fine (window open at baseline time); scope stored as Notepad.
    monkeypatch.setattr("agent_windows.core.tree.resolve_window",
                        lambda value=None, active=False: (None, ""))
    HANDLERS["snapshot_diff"]({"action": "save", "window": "Notepad"})
    # Window closed between save and diff → resolve_window raises ValueError.
    def _raise(value=None, active=False):
        raise ValueError("Window not found: Notepad")
    monkeypatch.setattr("agent_windows.core.tree.resolve_window", _raise)
    with pytest.raises(ValueError, match="Window not found"):
        HANDLERS["snapshot_diff"]({"action": "diff"})
