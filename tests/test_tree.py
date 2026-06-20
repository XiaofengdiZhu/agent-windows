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

from agent_windows.core import tree
from agent_windows.core.tree import (
    _traverse_node,
    _traverse_window,
    resolve_element,
    ElementInfo,
    WindowInfo,
    BoundingBox,
    INTERACTIVE_TYPES,
)
from agent_windows.uia.enums import PropertyId
from agent_windows.uia.core import ControlType

BUTTON = ControlType.ButtonControl   # 50000 → "ButtonControl" (interactive)
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
                 children=None, raise_on=None, raise_on_build_updated=False):
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
    def __init__(self, root):
        self._root = root
        self.RawViewCondition = object()  # opaque; traversal just forwards it

    def ElementFromHandleBuildCache(self, handle, cache):
        return self._root

    def CreateCacheRequest(self):
        return FakeCacheRequest()


class FakeClient:
    def __init__(self, root):
        self.IUIAutomation = FakeIUIAutomation(root)


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
