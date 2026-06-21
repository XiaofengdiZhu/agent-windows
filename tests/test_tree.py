"""Deterministic unit tests for core/tree.py traversal internals.

These are the largest untested critical paths in the codebase — every consumer
in the test suite patches `core.tree.resolve_element`, so _traverse_node /
_traverse_window / resolve_element's parsing have ZERO real coverage. This file
builds a FAKE UIA element tree in pure Python and drives the real traversal
logic against it: no comtypes, no Win32, no real desktop, no cache, fully
deterministic. Each test targets a logic branch the mocks hide.

The fake surface is tiny and verified against the actual code:
- node.GetCachedPropertyValue(prop_id) for 6 property IDs (name, control-type
  int, is_enabled, is_offscreen, bounding-rect (l,t,w,h)-tuple, has_keyboard_focus)
- node.BuildUpdatedCache(children_req) → a wrapper whose GetCachedChildren()
  returns the node's children as an element array (PERF3-05 bulk path)
- client.IUIAutomation.ElementFromHandleBuildCache(handle, cache) → root (only
  _traverse_window uses this)
The cache_request arg is passed through opaquely and ignored by the fakes;
_get_cached_children calls cache_request.Clone() (returns self) and sets
TreeScope on the clone, both no-ops on the fake.
(TEST3-01)
"""
import time
from unittest.mock import patch, MagicMock

import pytest
from _ctypes import COMError

from agent_windows.core import tree
from agent_windows.core.tree import (
    _traverse_node,
    _traverse_window,
    traverse_tree,
    render_snapshot,
    resolve_element,
    ElementInfo,
    WindowInfo,
    BoundingBox,
    INTERACTIVE_TYPES,
)
from agent_windows.uia.enums import PropertyId, UIAError
from agent_windows.uia.core import ControlType

BUTTON = ControlType.ButtonControl   # 50000 → "ButtonControl" (interactive)
WINDOW = ControlType.WindowControl   # 50032 → "WindowControl" (modal-detection)
PANE = ControlType.PaneControl       # 50033 → "PaneControl" (NOT interactive)
TEXT = ControlType.TextControl       # 50020 → "TextControl" (NOT interactive)
EDIT = ControlType.EditControl       # 50004 → "EditControl" (interactive)

# A normal on-screen rect (left, top, WIDTH, HEIGHT) — UIA returns width/height,
# NOT right/bottom edges (see tree.py:425-440).
RECT_ONSCREEN = (10, 20, 100, 50)
RECT_ZERO_AREA = (5, 5, 0, 0)


class FakeElement:
    """A fake UIA element. GetCachedPropertyValue returns per-property values;
    BuildUpdatedCache exposes the element's children for the bulk path."""

    def __init__(self, *, name="", control_type=TEXT, is_enabled=True,
                 is_offscreen=False, rect=RECT_ONSCREEN, has_keyboard_focus=False,
                 children=None, raise_on=None, raise_on_build_updated=False,
                 is_modal=False):
        self._name = name
        self._control_type = control_type
        self._is_enabled = is_enabled
        self._is_offscreen = is_offscreen
        self._rect = rect
        self._has_focus = has_keyboard_focus
        self.children = list(children) if children else []
        # If set, GetCachedPropertyValue raises for this property id (QUAL-02 test)
        self._raise_on = raise_on
        # If True, BuildUpdatedCache raises (PERF3-05 child-fetch failure test)
        self._raise_on_build_updated = raise_on_build_updated
        # WindowIsModalProperty value (PERF3-06 modal-marking test)
        self._is_modal = is_modal

    def GetCachedPropertyValue(self, prop_id):
        if self._raise_on == prop_id:
            raise RuntimeError("simulated stale cache read")
        if prop_id == PropertyId.NameProperty:
            return self._name or None
        if prop_id == PropertyId.ControlTypeProperty:
            return self._control_type
        if prop_id == PropertyId.IsEnabledProperty:
            return self._is_enabled
        if prop_id == PropertyId.IsOffscreenProperty:
            return self._is_offscreen
        if prop_id == PropertyId.BoundingRectangleProperty:
            return self._rect
        if prop_id == PropertyId.HasKeyboardFocusProperty:
            return self._has_focus
        if prop_id == PropertyId.WindowIsModalProperty:
            return self._is_modal
        return None

    def BuildUpdatedCache(self, children_req):
        """Bulk-fetch entry point (PERF3-05). Returns a wrapper whose
        GetCachedChildren() yields this element's children as an array."""
        if self._raise_on_build_updated:
            raise RuntimeError("BuildUpdatedCache exploded")
        return _UpdatedElement(self.children)


class _UpdatedElement:
    """Wraps the result of BuildUpdatedCache; GetCachedChildren returns the
    parent's children in the IUIAutomationElementArray shape (.Length / .GetElement)."""

    def __init__(self, children):
        self._children = children

    def GetCachedChildren(self):
        return FakeElementArray(self._children) if self._children else None


class FakeElementArray:
    """Minimal IUIAutomationElementArray stand-in."""

    def __init__(self, elements):
        self._elements = list(elements)

    @property
    def Length(self):
        return len(self._elements)

    def GetElement(self, index):
        return self._elements[index]


class FakeCacheRequest:
    """Opaque cache object. Clone() returns itself; TreeScope/TreeFilter/AddProperty
    are accepted as no-ops — the real traversal only needs Clone() to succeed and
    TreeScope to be assignable on the clone."""

    def __init__(self):
        self.TreeScope = 0
        self.TreeFilter = None

    def AddProperty(self, *_):
        pass

    def AddPattern(self, *_):
        pass

    def Clone(self):
        return FakeCacheRequest()


class FakeIUIAutomation:
    def __init__(self, root, element_side_effects=None):
        self._root = root
        self.RawViewCondition = object()  # opaque; traversal just forwards it
        # Optional per-call override for ElementFromHandleBuildCache: a list of
        # return values / exceptions consumed in order (MagicMock side_effect
        # style). Exhausted or None → return self._root. Used by retry tests.
        self._side_effects = list(element_side_effects) if element_side_effects else None

    def ElementFromHandleBuildCache(self, handle, cache):
        if self._side_effects:
            val = self._side_effects.pop(0)
            if isinstance(val, Exception):
                raise val
            return val
        return self._root

    def CreateCacheRequest(self):
        return FakeCacheRequest()


class FakeClient:
    def __init__(self, root, element_side_effects=None):
        self.IUIAutomation = FakeIUIAutomation(root, element_side_effects)


def _link_siblings(elements):
    """No-op since PERF3-05: the bulk path reads children via BuildUpdatedCache,
    not a sibling-chaining walker. Kept so existing tests that wire up deep trees
    still read clearly; the sibling chain itself is unused."""
    return elements


def traverse(client, root, *, max_depth=15, include_invisible=False,
             include_offscreen=False, max_elements=0, timeout=10.0,
             start_time=None, global_deadline=float("inf")):
    """Drive _traverse_node directly on `root` at depth 0; return (lines, interactive)."""
    lines = ["Desktop", '  └─ Window "FakeWin" (fake) [active]']
    interactive = []
    win = WindowInfo(title="FakeWin", process_name="fake", handle=1,
                     bbox=BoundingBox(0, 0, 800, 600), is_active=True)
    # Build a real FakeCacheRequest via the production helper so _get_cached_children
    # can Clone() it and read TreeScope (PERF3-05 bulk path needs a non-None request).
    cache_request = tree._build_traversal_cache(client)
    _traverse_node(client, root, lines, interactive, win,
                   max_depth=max_depth, include_invisible=include_invisible,
                   include_offscreen=include_offscreen, indent=4, depth=0,
                   start_time=start_time if start_time is not None else time.time(),
                   timeout=timeout,
                   max_elements=max_elements, global_deadline=global_deadline,
                   cache_request=cache_request)
    return lines, interactive


# ── interactivity classification ─────────────────────────────────────


class TestInteractivity:
    def test_interactive_button_collected(self):
        root = FakeElement(control_type=PANE, children=[
            FakeElement(name="OK", control_type=BUTTON),
        ])
        _link_siblings(root.children)
        _, interactive = traverse(FakeClient(root), root)
        assert len(interactive) == 1
        assert interactive[0].name == "OK"
        assert interactive[0].control_type == "ButtonControl"
        assert interactive[0].is_interactive is True
        assert interactive[0].is_enabled is True

    def test_disabled_button_not_interactive(self):
        root = FakeElement(control_type=PANE, children=[
            FakeElement(name="OK", control_type=BUTTON, is_enabled=False),
        ])
        _link_siblings(root.children)
        _, interactive = traverse(FakeClient(root), root)
        assert interactive == []  # disabled → not interactive

    def test_noninteractive_pane_not_collected(self):
        root = FakeElement(control_type=PANE, children=[
            FakeElement(name="pane", control_type=PANE),
        ])
        _link_siblings(root.children)
        _, interactive = traverse(FakeClient(root), root)
        assert interactive == []  # Pane not in INTERACTIVE_TYPES

    def test_root_depth0_never_added_as_element(self):
        """The window root (depth 0) is skipped from ElementInfo (tree.py:462-463)."""
        root = FakeElement(name="rootbtn", control_type=BUTTON)  # interactive type
        _, interactive = traverse(FakeClient(root), root)
        assert interactive == []

    def test_edit_control_interactive(self):
        root = FakeElement(control_type=PANE, children=[
            FakeElement(name="field", control_type=EDIT),
        ])
        _link_siblings(root.children)
        _, interactive = traverse(FakeClient(root), root)
        assert len(interactive) == 1
        assert interactive[0].control_type == "EditControl"


# ── depth limit ──────────────────────────────────────────────────────


class TestDepthLimit:
    def test_max_depth_limits_descent(self):
        # 4-level chain of interactive buttons: root(pane) -> d1 -> d2 -> d3 -> d4
        # Using BUTTON (interactive) so each visited node lands in `interactive`.
        e4 = FakeElement(name="d4", control_type=BUTTON, children=[])
        e3 = FakeElement(name="d3", control_type=BUTTON, children=[e4])
        e2 = FakeElement(name="d2", control_type=BUTTON, children=[e3])
        e1 = FakeElement(name="d1", control_type=BUTTON, children=[e2])
        root = FakeElement(control_type=PANE, children=[e1])
        # Link siblings at every level so the walker can chain forward.
        for parent, kids in [(root, [e1]), (e1, [e2]), (e2, [e3]), (e3, [e4])]:
            _link_siblings(kids)
            for k in kids:
                if k.children:
                    _link_siblings(k.children)

        # max_depth=N means depth >= N returns (tree.py:384). So max_depth=2 visits
        # depth 0 (root, skipped from interactive) and depth 1 (d1); d2 at depth 2
        # is rejected. d3/d4 deeper still are not reached.
        _, interactive = traverse(FakeClient(root), root, max_depth=2)
        names = [e.name for e in interactive]
        assert names == ["d1"], f"expected only d1 at max_depth=2, got {names}"

        # max_depth=3 reaches d2 (depth 2 < 3) but not d3 (depth 3 >= 3).
        _, interactive = traverse(FakeClient(root), root, max_depth=3)
        names = [e.name for e in interactive]
        assert names == ["d1", "d2"], f"expected d1,d2 at max_depth=3, got {names}"

        # Unbounded reaches all.
        _, interactive = traverse(FakeClient(root), root, max_depth=15)
        assert [e.name for e in interactive] == ["d1", "d2", "d3", "d4"]


# ── filters: offscreen, zero-area ────────────────────────────────────


class TestFilters:
    def test_offscreen_dropped_by_default(self):
        root = FakeElement(control_type=PANE, children=[
            FakeElement(name="on", control_type=BUTTON, is_offscreen=False),
            FakeElement(name="off", control_type=BUTTON, is_offscreen=True),
        ])
        _link_siblings(root.children)
        _, interactive = traverse(FakeClient(root), root)
        assert [e.name for e in interactive] == ["on"]

    def test_offscreen_kept_when_include_offscreen_true(self):
        root = FakeElement(control_type=PANE, children=[
            FakeElement(name="off", control_type=BUTTON, is_offscreen=True),
        ])
        _link_siblings(root.children)
        _, interactive = traverse(FakeClient(root), root, include_offscreen=True)
        assert [e.name for e in interactive] == ["off"]

    def test_zero_area_dropped_by_default(self):
        """include_invisible actually gates ZERO-AREA boxes (tree.py:445), not an
        IsVisible property — lock this surprising-but-stable behavior."""
        root = FakeElement(control_type=PANE, children=[
            FakeElement(name="zero", control_type=BUTTON, rect=RECT_ZERO_AREA),
            FakeElement(name="normal", control_type=BUTTON),
        ])
        _link_siblings(root.children)
        _, interactive = traverse(FakeClient(root), root)
        assert [e.name for e in interactive] == ["normal"]

    def test_zero_area_kept_when_include_invisible_true(self):
        root = FakeElement(control_type=PANE, children=[
            FakeElement(name="zero", control_type=BUTTON, rect=RECT_ZERO_AREA),
        ])
        _link_siblings(root.children)
        _, interactive = traverse(FakeClient(root), root, include_invisible=True)
        assert [e.name for e in interactive] == ["zero"]


# ── max_elements cap ─────────────────────────────────────────────────


class TestMaxElements:
    def test_cap_stops_after_limit(self):
        root = FakeElement(control_type=PANE, children=[
            FakeElement(name=f"b{i}", control_type=BUTTON) for i in range(5)
        ])
        _link_siblings(root.children)
        _, interactive = traverse(FakeClient(root), root, max_elements=2)
        # Stops once len(interactive) reaches 2.
        assert len(interactive) == 2

    def test_cap_zero_disables(self):
        root = FakeElement(control_type=PANE, children=[
            FakeElement(name=f"b{i}", control_type=BUTTON) for i in range(5)
        ])
        _link_siblings(root.children)
        _, interactive = traverse(FakeClient(root), root, max_elements=0)
        assert len(interactive) == 5


# ── time bounds: per-window timeout + global deadline ────────────────


class TestTimeBounds:
    """The timeout/global_deadline guards (tree.py:386-389) are production safety
    bounds that stop a runaway traversal. Lock the early-returns deterministically
    by passing an already-expired budget."""

    def test_per_window_timeout_expired_skips_node(self):
        """start_time far in the past + small timeout → (time.time() - start_time)
        > timeout → node returns immediately (tree.py:386-387)."""
        root = FakeElement(control_type=PANE, children=[
            FakeElement(name="b", control_type=BUTTON),
        ])
        _link_siblings(root.children)
        _, interactive = traverse(FakeClient(root), root,
                                  start_time=time.time() - 1000, timeout=1.0)
        assert interactive == []  # node skipped before any child is processed

    def test_global_deadline_expired_skips_node(self):
        """global_deadline in the past → time.time() > global_deadline → return
        (tree.py:388-389)."""
        root = FakeElement(control_type=PANE, children=[
            FakeElement(name="b", control_type=BUTTON),
        ])
        _link_siblings(root.children)
        _, interactive = traverse(FakeClient(root), root,
                                  global_deadline=time.time() - 1)
        assert interactive == []


# ── tree-line rendering ──────────────────────────────────────────────


class TestRendering:
    def test_indentation_and_labels(self):
        root = FakeElement(control_type=PANE, children=[
            FakeElement(name="OK", control_type=BUTTON, has_keyboard_focus=True),
        ])
        _link_siblings(root.children)
        lines, _ = traverse(FakeClient(root), root)
        joined = "\n".join(lines)
        # Control type label has "Control" stripped; name quoted; focused marked.
        assert 'Button "OK"' in joined
        assert "[focused]" in joined
        assert "[interactive]" in joined
        # Indented under the window line (4 spaces from traverse() indent=4)
        button_line = [l for l in lines if 'Button "OK"' in l][0]
        assert button_line.startswith("    ├─ ")

    def test_disabled_label(self):
        root = FakeElement(control_type=PANE, children=[
            FakeElement(name="x", control_type=BUTTON, is_enabled=False),
        ])
        _link_siblings(root.children)
        lines, _ = traverse(FakeClient(root), root)
        joined = "\n".join(lines)
        assert "[disabled]" in joined
        assert "[interactive]" not in joined  # disabled → not interactive


# ── QUAL-02 except-swallow graceful degradation ──────────────────────


class TestExceptSwallow:
    def test_property_read_failure_degrades_to_default(self):
        """A stale/missing cached property must degrade to its default and NOT
        abort the node — this is the QUAL-02 fix (bare except → logger.debug
        with defaults). Here IsEnabled raises → defaults True → still interactive."""
        root = FakeElement(control_type=PANE, children=[
            FakeElement(name="OK", control_type=BUTTON,
                        raise_on=PropertyId.IsEnabledProperty),
        ])
        _link_siblings(root.children)
        _, interactive = traverse(FakeClient(root), root)
        # Despite the exception, the node is processed: is_enabled defaulted True.
        assert len(interactive) == 1
        assert interactive[0].is_enabled is True

    def test_bbox_failure_keeps_traversal_alive(self):
        root = FakeElement(control_type=PANE, children=[
            FakeElement(name="OK", control_type=BUTTON,
                        raise_on=PropertyId.BoundingRectangleProperty),
        ])
        _link_siblings(root.children)
        # Should not raise; bbox defaults None, no zero-area filter trips.
        _, interactive = traverse(FakeClient(root), root)
        assert len(interactive) == 1
        assert interactive[0].bbox is None


# ── _traverse_window ─────────────────────────────────────────────────


class TestTraverseWindow:
    def _win(self):
        return WindowInfo(title="FakeWin", process_name="fake", handle=1,
                          bbox=BoundingBox(0, 0, 800, 600), is_active=True)

    def test_builds_root_from_handle_and_traverses(self):
        root = FakeElement(control_type=PANE, children=[
            FakeElement(name="OK", control_type=BUTTON),
        ])
        _link_siblings(root.children)
        client = FakeClient(root)
        lines = []
        interactive = []
        _traverse_window(client, self._win(), lines, interactive,
                         max_depth=15, include_invisible=False,
                         include_offscreen=False, indent=4, timeout=10.0,
                         max_elements=0, global_deadline=float("inf"),
                         cache_request=tree._build_traversal_cache(client))
        assert len(interactive) == 1
        assert interactive[0].name == "OK"

    def test_element_from_handle_none_returns_silently(self):
        """If ElementFromHandleBuildCache returns None (no root), _traverse_window
        returns without crashing or adding elements (tree.py:336-337)."""
        client = FakeClient(None)  # root is None
        interactive = []
        _traverse_window(client, self._win(), [], interactive,
                         max_depth=15, include_invisible=False,
                         include_offscreen=False, indent=4, timeout=10.0,
                         max_elements=0, global_deadline=float("inf"),
                         cache_request=None)
        assert interactive == []

    def test_element_from_handle_raises_returns_silently(self):
        """A COM failure building the root is swallowed (tree.py:332-334)."""
        class BoomClient:
            IUIAutomation = MagicMock()
        BoomClient.IUIAutomation.ElementFromHandleBuildCache.side_effect = \
            RuntimeError("com died")
        client = BoomClient()
        interactive = []
        _traverse_window(client, self._win(), [], interactive,
                         max_depth=15, include_invisible=False,
                         include_offscreen=False, indent=4, timeout=10.0,
                         max_elements=0, global_deadline=float("inf"),
                         cache_request=None)
        assert interactive == []


# ── resolve_element @eN parsing ──────────────────────────────────────


class TestResolveElementParsing:
    """resolve_element parses @eN then reads interactive_elements from the
    snapshot. We monkeypatch get_tree_snapshot + load_config to avoid real UIA."""

    def _patch_snapshot(self, elements):
        return patch("agent_windows.core.tree.get_tree_snapshot",
                     return_value={"interactive_elements": elements})

    def _patch_config(self):
        return patch("agent_windows.utils.config.load_config", return_value={})

    def test_valid_selector_returns_element(self):
        els = [ElementInfo(name=f"e{i}", control_type="ButtonControl",
                           bbox=None) for i in range(5)]
        with self._patch_snapshot(els), self._patch_config():
            result = resolve_element("@e3")
        assert result.name == "e2"  # 1-based → 0-based

    def test_first_selector(self):
        els = [ElementInfo(name="first", control_type="ButtonControl", bbox=None)]
        with self._patch_snapshot(els), self._patch_config():
            assert resolve_element("@e1").name == "first"

    @pytest.mark.parametrize("bad", ["", "e3", "@e", "@x5", "e", "@e-1", "@E3"])
    def test_invalid_format_raises_valueerror(self, bad):
        with self._patch_snapshot([]), self._patch_config():
            with pytest.raises(ValueError, match="Invalid selector"):
                resolve_element(bad)

    def test_negative_index_raises_valueerror(self):
        # @e0 → index -1 (1-based means @e0 is invalid)
        els = [ElementInfo(name="x", control_type="ButtonControl", bbox=None)]
        with self._patch_snapshot(els), self._patch_config():
            with pytest.raises(ValueError, match="Invalid selector"):
                resolve_element("@e0")

    def test_out_of_range_raises_indexerror(self):
        els = [ElementInfo(name="only", control_type="ButtonControl", bbox=None)]
        with self._patch_snapshot(els), self._patch_config():
            with pytest.raises(IndexError, match="not found"):
                resolve_element("@e5")

    def test_non_integer_suffix_raises_valueerror(self):
        with self._patch_snapshot([]), self._patch_config():
            with pytest.raises(ValueError, match="Invalid selector"):
                resolve_element("@eabc")


# ── child-iteration failure is swallowed (QUAL-02 outer block) ───────


class TestChildIterationSwallow:
    def test_child_fetch_failure_parent_still_processed(self):
        """If _get_cached_children fails while fetching an element's children
        (BuildUpdatedCache raises), the element itself is STILL collected (it
        was processed before recursion), and the traversal does not crash.
        This locks the PERF3-05 "parent still processed, subtree pruned"
        guarantee — the previous assertion (interactive == []) could not
        distinguish this from a full-node abort."""
        # root(Pane) -> child(Button, interactive, has its own grandchild).
        # The Button is collected BEFORE its children are fetched; making the
        # Button's BuildUpdatedCache raise prunes the grandchild, but the Button
        # must still appear in `interactive`.
        grandchild = FakeElement(name="gc", control_type=BUTTON, children=[])
        child = FakeElement(name="parent-btn", control_type=BUTTON,
                            children=[grandchild], raise_on_build_updated=True)
        root = FakeElement(control_type=PANE, children=[child])
        _, interactive = traverse(FakeClient(root), root)
        assert [e.name for e in interactive] == ["parent-btn"]


# ── window-level retry on busy target (PERF3-06) ────────────────────


class TestWindowRetry:
    def _win(self):
        return WindowInfo(title="FakeWin", process_name="fake", handle=1,
                          bbox=BoundingBox(0, 0, 800, 600), is_active=True)

    def _retryable_comerror(self):
        # RPC_E_SERVERCALL_RETRYLATER — target busy but alive, retryable.
        return COMError(UIAError.RPC_E_SERVERCALL_RETRYLATER, "busy", (None, None, None, None, None))

    def test_retryable_error_then_success(self, monkeypatch):
        """A retryable COMError on the first root-build attempt is retried; the
        window is traversed on the second attempt and elements are collected."""
        monkeypatch.setattr(time, "sleep", lambda _: None)  # no real backoff in tests
        root = FakeElement(control_type=PANE, children=[
            FakeElement(name="OK", control_type=BUTTON),
        ])
        # First call raises (busy), second returns the root.
        client = FakeClient(root, element_side_effects=[self._retryable_comerror()])
        interactive = []
        _traverse_window(client, self._win(), [], interactive,
                         max_depth=15, include_invisible=False,
                         include_offscreen=False, indent=4, timeout=10.0,
                         max_elements=0, global_deadline=float("inf"),
                         cache_request=tree._build_traversal_cache(client))
        assert [e.name for e in interactive] == ["OK"]

    def test_non_retryable_comerror_not_retried(self, monkeypatch):
        """A dead-element COMError (window gone) is terminal — no retry, window
        abandoned. Catches the case where retrying would only waste time."""
        monkeypatch.setattr(time, "sleep", lambda _: None)
        calls = {"n": 0}

        class DeadElementIUA:
            RawViewCondition = object()

            def ElementFromHandleBuildCache(self, handle, cache):
                calls["n"] += 1
                # RPC_E_DISCONNECTED — dead element, NOT retryable
                raise COMError(UIAError.RPC_E_DISCONNECTED, "gone",
                               (None, None, None, None, None))

            def CreateCacheRequest(self):
                return FakeCacheRequest()

        class DeadElementClient:
            def __init__(self):
                self.IUIAutomation = DeadElementIUA()

        interactive = []
        _traverse_window(DeadElementClient(), self._win(), [], interactive,
                         max_depth=15, include_invisible=False,
                         include_offscreen=False, indent=4, timeout=10.0,
                         max_elements=0, global_deadline=float("inf"),
                         cache_request=FakeCacheRequest())
        assert interactive == []
        assert calls["n"] == 1  # no retry — dead element is terminal

    def test_non_com_error_not_retried(self, monkeypatch):
        """A plain RuntimeError (not a COMError) is not retried — only COM
        busy-signal errors are. Locks the except-branch split."""
        monkeypatch.setattr(time, "sleep", lambda _: None)
        root = FakeElement(control_type=PANE, children=[
            FakeElement(name="OK", control_type=BUTTON),
        ])
        client = FakeClient(root, element_side_effects=[RuntimeError("com died")])
        interactive = []
        _traverse_window(client, self._win(), [], interactive,
                         max_depth=15, include_invisible=False,
                         include_offscreen=False, indent=4, timeout=10.0,
                         max_elements=0, global_deadline=float("inf"),
                         cache_request=tree._build_traversal_cache(client))
        assert interactive == []

    def test_retryable_exhausted_gives_up_gracefully(self, monkeypatch):
        """When EVERY attempt raises a retryable error, the window is abandoned
        after _WINDOW_MAX_RETRIES+1 total attempts — no crash, no elements. Locks
        the exhaustion path (the success/no-retry paths are covered above)."""
        monkeypatch.setattr(time, "sleep", lambda _: None)
        calls = {"n": 0}

        class AlwaysBusyIUA:
            RawViewCondition = object()

            def ElementFromHandleBuildCache(self, handle, cache):
                calls["n"] += 1
                raise COMError(UIAError.RPC_E_SERVERCALL_RETRYLATER, "busy",
                               (None, None, None, None, None))

            def CreateCacheRequest(self):
                return FakeCacheRequest()

        class AlwaysBusyClient:
            def __init__(self):
                self.IUIAutomation = AlwaysBusyIUA()

        interactive = []
        _traverse_window(AlwaysBusyClient(), self._win(), [], interactive,
                         max_depth=15, include_invisible=False,
                         include_offscreen=False, indent=4, timeout=10.0,
                         max_elements=0, global_deadline=float("inf"),
                         cache_request=FakeCacheRequest())
        assert interactive == []
        # 1 initial + _WINDOW_MAX_RETRIES retries = 4 total attempts.
        assert calls["n"] == tree._WINDOW_MAX_RETRIES + 1

    def test_backoff_aborts_when_exceeding_global_deadline(self, monkeypatch):
        """Retry backoff must not eat the global wall-clock budget: if the next
        sleep would blow global_deadline, the window is given up rather than
        sleeping past the budget meant for the remaining windows."""
        sleeps = []
        monkeypatch.setattr(time, "sleep", lambda s: sleeps.append(s))
        calls = {"n": 0}

        class AlwaysBusyIUA:
            RawViewCondition = object()

            def ElementFromHandleBuildCache(self, handle, cache):
                calls["n"] += 1
                raise COMError(UIAError.RPC_E_SERVERCALL_RETRYLATER, "busy",
                               (None, None, None, None, None))

            def CreateCacheRequest(self):
                return FakeCacheRequest()

        class AlwaysBusyClient:
            def __init__(self):
                self.IUIAutomation = AlwaysBusyIUA()

        interactive = []
        # global_deadline is NOW: the first 0.5s backoff would exceed it, so the
        # window is abandoned immediately without sleeping.
        _traverse_window(AlwaysBusyClient(), self._win(), [], interactive,
                         max_depth=15, include_invisible=False,
                         include_offscreen=False, indent=4, timeout=10.0,
                         max_elements=0, global_deadline=time.time(),
                         cache_request=FakeCacheRequest())
        assert interactive == []
        assert calls["n"] == 1  # only the initial attempt; no retry sleep taken
        assert sleeps == []      # backoff was skipped — deadline protected


# ── desktop (Progman) window subtree skip (PERF3-06) ────────────────


class TestProgmanSkip:
    def _win(self, *, class_name, title="Win", handle=1):
        return WindowInfo(title=title, process_name="p.exe", handle=handle,
                          bbox=BoundingBox(0, 0, 800, 600), class_name=class_name)

    def test_progman_window_subtree_skipped(self):
        """_should_skip_subtree returns True for the desktop window (class
        'Progman') — its icon-list subtree adds noise and is skipped. The skip is
        keyed on the locale-invariant class name, not the title."""
        assert tree._should_skip_subtree(self._win(class_name="Progman")) is True

    def test_normal_window_not_skipped(self):
        """A regular application window (any non-Progman class) is traversed."""
        assert tree._should_skip_subtree(self._win(class_name="AppFrame")) is False
        assert tree._should_skip_subtree(self._win(class_name="")) is False

    def test_skip_independent_of_localized_title(self):
        """The Progman window's title localizes (e.g. '桌面' on zh-CN); the skip
        must not depend on it, only on the stable class name."""
        win = self._win(class_name="Progman", title="桌面")
        assert tree._should_skip_subtree(win) is True


# ── modal dialog marking + promotion (PERF3-06) ─────────────────────


class TestModalMarking:
    def test_modal_subtree_elements_marked_and_promoted(self):
        """A WindowControl with WindowIsModal=True marks every interactive element
        in its subtree is_modal=True. _promote_modal_elements (the production
        sort used by traverse_tree) promotes them to the front. Non-modal
        siblings keep their order and are NOT dropped."""
        # root(Pane) -> [normalBtn, modalWindow(OK btn)]
        normal_btn = FakeElement(name="normalBtn", control_type=BUTTON)
        modal_btn = FakeElement(name="dialogOK", control_type=BUTTON)
        modal_window = FakeElement(name="Confirm", control_type=WINDOW,
                                   is_modal=True, children=[modal_btn])
        root = FakeElement(control_type=PANE, children=[normal_btn, modal_window])

        _, interactive = traverse(FakeClient(root), root)
        # Collection order: normalBtn first, then dialogOK (traversal order).
        assert [e.name for e in interactive] == ["normalBtn", "dialogOK"]
        # Marking: only the modal-subtree element is flagged.
        by_name = {e.name: e for e in interactive}
        assert by_name["dialogOK"].is_modal is True
        assert by_name["normalBtn"].is_modal is False

        # Call the PRODUCTION promotion function (not a copy of its logic) so a
        # logic change in tree.py would fail this test.
        tree._promote_modal_elements(interactive)
        assert [e.name for e in interactive] == ["dialogOK", "normalBtn"]

    def test_promote_modal_is_stable(self):
        """_promote_modal_elements is a STABLE sort: within the modal group and
        within the non-modal group, original order is preserved."""
        a = ElementInfo(name="a", control_type="ButtonControl", bbox=None, is_modal=False)
        b = ElementInfo(name="b", control_type="ButtonControl", bbox=None, is_modal=True)
        c = ElementInfo(name="c", control_type="ButtonControl", bbox=None, is_modal=False)
        d = ElementInfo(name="d", control_type="ButtonControl", bbox=None, is_modal=True)
        elements = [a, b, c, d]
        tree._promote_modal_elements(elements)
        assert [e.name for e in elements] == ["b", "d", "a", "c"]

    def test_non_modal_window_not_marked(self):
        """A WindowControl that is NOT modal does not mark its subtree."""
        win_btn = FakeElement(name="winBtn", control_type=BUTTON)
        non_modal_window = FakeElement(name="NormalWin", control_type=WINDOW,
                                       is_modal=False, children=[win_btn])
        root = FakeElement(control_type=PANE, children=[non_modal_window])
        _, interactive = traverse(FakeClient(root), root)
        assert all(not e.is_modal for e in interactive)

    def test_render_shows_modal_label_and_state(self):
        """The semantic tree tags a modal Window node with [modal], and the
        interactive-elements table shows 'modal' in the State column."""
        modal_btn = FakeElement(name="dialogOK", control_type=BUTTON)
        modal_window = FakeElement(name="Confirm", control_type=WINDOW,
                                   is_modal=True, children=[modal_btn])
        root = FakeElement(control_type=PANE, children=[modal_window])
        lines, interactive = traverse(FakeClient(root), root)
        # Tree line for the modal window carries [modal].
        tree_text = "\n".join(lines)
        assert "[modal]" in tree_text
        # Promote + render; the modal element's State column shows 'modal'.
        interactive.sort(key=lambda el: 0 if el.is_modal else 1)
        rendered = render_snapshot({
            "cursor": (0, 0), "resolution": (1920, 1080), "displays": 1,
            "active_window": None, "windows": [], "semantic_tree": tree_text,
            "interactive_elements": interactive,
        })
        assert "| modal |" in rendered


# ── snapshot metadata: virtual screen / dpi scale / coordinate space (TODO #3)


def _render_meta(**overrides):
    """Minimal render_snapshot input with the new metadata fields, overridable."""
    base = {
        "cursor": (100, 200),
        "resolution": (1920, 1080),
        "virtual_screen": (-1920, 0, 3840, 1080),
        "dpi_scale": 1.5,
        "displays": 2,
        "active_window": None,
        "windows": [],
        "semantic_tree": "",
        "interactive_elements": [],
    }
    base.update(overrides)
    return render_snapshot(base)


class TestSnapshotMetadataRender:
    def test_virtual_screen_line_rendered(self):
        rendered = _render_meta()
        assert "Virtual Screen: (-1920, 0, 3840, 1080)" in rendered

    @pytest.mark.parametrize("scale, dpi", [(1.5, 144), (1.25, 120), (1.0, 96)])
    def test_dpi_scale_line_with_computed_dpi(self, scale, dpi):
        assert f"DPI Scale: {scale:g} ({dpi} dpi)" in _render_meta(dpi_scale=scale)

    def test_coordinate_space_line_always_present(self):
        rendered = _render_meta()
        assert "Coordinate Space: Virtual desktop coordinates (physical pixels)" in rendered

    def test_metadata_after_basic_lines_before_active_window(self):
        """Virtual Screen / DPI / Coordinate Space appear in the Desktop State
        block; existing Cursor/Resolution/Displays lines are unchanged and still
        precede the new metadata."""
        rendered = _render_meta(active_window=WindowInfo(
            title="App", process_name="app", handle=1,
            bbox=BoundingBox(0, 0, 10, 10), is_active=True))
        i_cursor = rendered.index("Cursor:")
        i_res = rendered.index("Resolution:")
        i_disp = rendered.index("Displays:")
        i_vs = rendered.index("Virtual Screen:")
        i_dpi = rendered.index("DPI Scale:")
        i_cs = rendered.index("Coordinate Space:")
        i_active = rendered.index("Active Window:")
        # Order: cursor < resolution < displays < vscreen < dpi < coord-space < active
        assert i_cursor < i_res < i_disp < i_vs < i_dpi < i_cs < i_active

    def test_legacy_snapshot_without_new_fields_does_not_crash(self):
        """render_snapshot must tolerate a dict missing virtual_screen/dpi_scale
        (e.g. a cached snapshot from before this change) — the new lines are
        skipped, not raised on."""
        rendered = render_snapshot({
            "cursor": (0, 0), "resolution": (1920, 1080), "displays": 1,
            "active_window": None, "windows": [], "semantic_tree": "",
            "interactive_elements": [],
        })
        assert "Coordinate Space:" in rendered  # always-present line still shows
        assert "Virtual Screen:" not in rendered  # field absent → line absent
        assert "DPI Scale:" not in rendered


class TestTraverseTreeMetadata:
    """traverse_tree must plumb virtual_screen/dpi_scale into its return dict.
    Drives the real function with its Win32 / UIA entry points patched so no
    real desktop access happens."""

    def _patched_traverse(self):
        # timeout=0.0 → global_deadline is already past, so the per-window
        # traversal loop body never runs and no real UIA work happens.
        # enumerate_windows is separately patched to return [] so no windows are
        # even listed.
        return traverse_tree(
            max_depth=1, include_invisible=False, include_offscreen=False,
            max_elements=0, timeout=0.0,
        )

    def test_return_dict_has_virtual_screen_and_dpi_scale(self):
        # tree.py imports get_virtual_screen_rect / get_dpi_scale lazily INSIDE
        # traverse_tree (`from agent_windows.core.screenshot import ...`), so
        # patching the canonical module attribute is what the function sees at
        # call time. _AutomationClient is likewise imported lazily inside the
        # function, so patch at its source module.
        with patch("agent_windows.uia.core._AutomationClient"), \
             patch("agent_windows.core.tree.enumerate_windows", return_value=[]), \
             patch("agent_windows.core.screenshot.get_virtual_screen_rect",
                   return_value=(-1920, 0, 3840, 1080)) as m_vs, \
             patch("agent_windows.core.screenshot.get_dpi_scale",
                   return_value=1.5) as m_dpi:
            data = self._patched_traverse()
        assert data["virtual_screen"] == (-1920, 0, 3840, 1080)
        assert data["dpi_scale"] == 1.5
        m_vs.assert_called_once()
        m_dpi.assert_called_once()

    def test_existing_fields_still_present(self):
        """Adding metadata did not drop cursor/resolution/displays/etc."""
        with patch("agent_windows.uia.core._AutomationClient"), \
             patch("agent_windows.core.tree.enumerate_windows", return_value=[]), \
             patch("agent_windows.core.screenshot.get_virtual_screen_rect",
                   return_value=(0, 0, 1920, 1080)), \
             patch("agent_windows.core.screenshot.get_dpi_scale",
                   return_value=1.0):
            data = self._patched_traverse()
        for key in ("cursor", "resolution", "displays", "active_window",
                    "windows", "semantic_tree", "interactive_elements"):
            assert key in data, f"{key} dropped from traverse_tree return"

