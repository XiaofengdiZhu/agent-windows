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
    resolve_window,
    ElementInfo,
    WindowInfo,
    BoundingBox,
    INTERACTIVE_TYPES,
)
from agent_windows.uia.enums import PropertyId, UIAError, ToggleState, ExpandCollapseState
from agent_windows.uia.core import ControlType

BUTTON = ControlType.ButtonControl   # 50000 → "ButtonControl" (interactive)
CHECKBOX = ControlType.CheckBoxControl  # 50002 → "CheckBoxControl"
COMBO = ControlType.ComboBoxControl     # 50003 → "ComboBoxControl"
EDIT = ControlType.EditControl          # 50004 → "EditControl" (interactive)
LIST_ITEM = ControlType.ListItemControl  # 50007 → "ListItemControl"
SLIDER = ControlType.SliderControl      # 50015 → "SliderControl"
SPINNER = ControlType.SpinnerControl    # 50016 → "SpinnerControl"
TREE_ITEM = ControlType.TreeItemControl  # 50024 → "TreeItemControl"
WINDOW = ControlType.WindowControl   # 50032 → "WindowControl" (modal-detection)
PANE = ControlType.PaneControl       # 50033 → "PaneControl" (NOT interactive)
TEXT = ControlType.TextControl       # 50020 → "TextControl" (NOT interactive)

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
                 is_modal=False, extra_props=None):
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
        # TODO #15: additional cached property values keyed by PropertyId attr.
        self._extra = dict(extra_props) if extra_props else {}

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
        return self._extra.get(prop_id)

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


# ── window_name plumbing (TODO #7 prerequisite) ─────────────────────


class TestElementWindowName:
    """_traverse_node must stamp each ElementInfo with the owning window's
    title (free — the WindowInfo is already a parameter), and render_snapshot
    must surface it as a Window column so WaitFor's window_name condition has
    data to match against."""

    def test_traverse_stamps_owning_window_title(self):
        root = FakeElement(control_type=PANE, children=[
            FakeElement(name="OK", control_type=BUTTON),
            FakeElement(name="Cancel", control_type=BUTTON),
        ])
        _link_siblings(root.children)
        _, interactive = traverse(FakeClient(root), root)
        assert len(interactive) == 2
        # The `traverse` helper builds WindowInfo(title="FakeWin", ...), so both
        # collected elements inherit that title.
        assert all(el.window_name == "FakeWin" for el in interactive)

    def test_default_window_name_empty_for_back_compat(self):
        """ElementInfo constructed without window_name defaults to "" so
        existing constructors (and the make_element fixture) stay compatible."""
        el = ElementInfo(name="x", control_type="ButtonControl", bbox=None)
        assert el.window_name == ""

    def test_render_snapshot_shows_window_column(self):
        el = ElementInfo(
            name="OK", control_type="ButtonControl",
            bbox=BoundingBox(0, 0, 10, 10), window_name="Settings")
        rendered = render_snapshot({
            "cursor": (0, 0), "resolution": (1920, 1080), "displays": 1,
            "active_window": None, "windows": [], "semantic_tree": "",
            "interactive_elements": [el],
        })
        assert "| Window |" in rendered  # header includes the new column
        assert "Settings" in rendered     # the element's owning window title

    def test_render_snapshot_empty_window_name_blank(self):
        """An element with no owning window renders an empty Window cell, not
        'None' or a crash — guards legacy ElementInfo without window_name."""
        el = ElementInfo(name="OK", control_type="ButtonControl", bbox=None)
        rendered = render_snapshot({
            "cursor": (0, 0), "resolution": (1920, 1080), "displays": 1,
            "active_window": None, "windows": [], "semantic_tree": "",
            "interactive_elements": [el],
        })
        assert "| Window |" in rendered


class TestWindowStatusRender:
    """The Windows-table Status column now reports placement (Maximized/
    Minimized/Normal) and folds in the active signal as '(active)' rather than
    only ever showing 'active' (TODO #2)."""

    def _render(self, windows):
        return render_snapshot({
            "cursor": (0, 0), "resolution": (1920, 1080), "displays": 1,
            "active_window": None, "windows": windows, "semantic_tree": "",
            "interactive_elements": [],
        })

    def test_minimized_window_shows_minimized_status(self):
        win = WindowInfo(title="W", process_name="app", handle=1,
                         bbox=BoundingBox(0, 0, 10, 10), status="Minimized")
        rendered = self._render([win])
        assert "| Minimized |" in rendered

    def test_maximized_and_active_shows_active_suffix(self):
        win = WindowInfo(title="W", process_name="app", handle=1,
                         bbox=BoundingBox(0, 0, 10, 10),
                         status="Maximized", is_active=True)
        rendered = self._render([win])
        assert "| Maximized (active) |" in rendered

    def test_normal_inactive_shows_plain_normal(self):
        win = WindowInfo(title="W", process_name="app", handle=1,
                         bbox=BoundingBox(0, 0, 10, 10), status="Normal")
        rendered = self._render([win])
        assert "| Normal |" in rendered
        assert "active" not in rendered  # not active → no suffix

    def test_browser_window_marked_in_app_cell(self):
        # is_browser is surfaced as a "(browser)" marker on the App cell so the
        # hint reaches the agent rather than staying dead data (TODO #2).
        win = WindowInfo(title="W", process_name="chrome.exe", handle=1,
                         bbox=BoundingBox(0, 0, 10, 10), is_browser=True)
        rendered = self._render([win])
        assert "| chrome.exe (browser) |" in rendered


class TestEnumerateWindowStatusFields:
    """enumerate_windows computes status (IsIconic/IsZoomed) and is_browser
    (process name match) per window (TODO #2). The logic is extracted into
    _window_status_from_placement / _is_browser_process so it's testable without
    driving the real EnumWindows callback (which needs live ctypes structs)."""

    def test_iconic_is_minimized(self):
        from agent_windows.core.tree import _window_status_from_placement as f
        assert f(is_iconic=True, is_zoomed=False) == "Minimized"

    def test_zoomed_is_maximized(self):
        from agent_windows.core.tree import _window_status_from_placement as f
        assert f(is_iconic=False, is_zoomed=True) == "Maximized"

    def test_neither_is_normal(self):
        from agent_windows.core.tree import _window_status_from_placement as f
        assert f(is_iconic=False, is_zoomed=False) == "Normal"

    def test_iconic_wins_over_zoomed(self):
        # Matches reference if/elif order; flags are mutually exclusive in practice.
        from agent_windows.core.tree import _window_status_from_placement as f
        assert f(is_iconic=True, is_zoomed=True) == "Minimized"

    def test_browser_detection_case_insensitive(self):
        from agent_windows.core.tree import _is_browser_process
        assert _is_browser_process("chrome.exe")
        assert _is_browser_process("Chrome.exe")   # case-insensitive
        assert _is_browser_process("MSEdge.exe")
        assert _is_browser_process("firefox.exe")
        assert not _is_browser_process("explorer.exe")
        assert not _is_browser_process("notepad.exe")
        assert not _is_browser_process("")


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


# ── resolve_element_center @eN → bbox center (TODO #13) ──────────────


class TestResolveElementCenter:
    """resolve_element_center returns the bbox center of an @eN element and
    re-validates it against the virtual screen. We mock the virtual screen so
    the in-bounds check is deterministic."""

    _VSCREEN = (0, 0, 1920, 1080)

    def _patch_snapshot(self, elements):
        return patch("agent_windows.core.tree.get_tree_snapshot",
                     return_value={"interactive_elements": elements})

    def _patch_config(self):
        return patch("agent_windows.utils.config.load_config", return_value={})

    def test_returns_bbox_center(self):
        from agent_windows.core.tree import resolve_element_center
        el = ElementInfo(name="btn", control_type="ButtonControl",
                         bbox=BoundingBox(100, 200, 300, 400))  # center (200, 300)
        with self._patch_snapshot([el]), self._patch_config(), \
                patch("agent_windows.core.input_controller._get_virtual_screen",
                      return_value=self._VSCREEN):
            x, y = resolve_element_center("@e1")
        assert (x, y) == (200, 300)

    def test_no_bbox_raises_valueerror(self):
        from agent_windows.core.tree import resolve_element_center
        el = ElementInfo(name="x", control_type="ButtonControl", bbox=None)
        with self._patch_snapshot([el]), self._patch_config():
            with pytest.raises(ValueError, match="no usable bounding box"):
                resolve_element_center("@e1")

    def test_zero_area_bbox_raises_valueerror(self):
        """A degenerate (zero-area) bbox is unusable for centering — reject it
        rather than clicking a corner pixel."""
        from agent_windows.core.tree import resolve_element_center
        el = ElementInfo(name="x", control_type="ButtonControl",
                         bbox=BoundingBox(50, 50, 50, 50))  # zero area
        with self._patch_snapshot([el]), self._patch_config():
            with pytest.raises(ValueError, match="no usable bounding box"):
                resolve_element_center("@e1")

    def test_stale_offscreen_center_revalidated(self):
        """A cached bbox whose center has gone off-screen (window moved/closed
        since the 2s-TTL snapshot) surfaces as CoordinateRangeError, not a
        silent misclick. (TODO #13 defensive re-validation.)"""
        from agent_windows.core.tree import resolve_element_center
        from agent_windows.daemon.router import CoordinateRangeError
        # bbox center at (50000, 50000) — way off a 1920x1080 screen
        el = ElementInfo(name="x", control_type="ButtonControl",
                         bbox=BoundingBox(49900, 49900, 50100, 50100))
        with self._patch_snapshot([el]), self._patch_config(), \
                patch("agent_windows.core.input_controller._get_virtual_screen",
                      return_value=self._VSCREEN):
            with pytest.raises(CoordinateRangeError):
                resolve_element_center("@e1")

    def test_invalid_selector_format_raises(self):
        from agent_windows.core.tree import resolve_element_center
        with self._patch_snapshot([]), self._patch_config():
            with pytest.raises(ValueError, match="Invalid selector"):
                resolve_element_center("not-a-selector")


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


class TestTraverseTreeProfiling:
    """traverse_tree emits per-stage timings (enumerate_ms / traverse_ms) to the
    log ONLY when is_debug() is on, and never otherwise (TODO #3). The hot path
    must pay nothing in production — verified by asserting no perf_counter work
    leaks when debug is off. Uses the same patched-surface approach as
    TestTraverseTreeMetadata (timeout=0.0 skips the real UIA walk)."""

    def _traverse_debug(self, debug_on, caplog):
        import logging
        from agent_windows.core import tree as tree_mod
        with patch.object(tree_mod, "enumerate_windows", return_value=[]), \
             patch.object(tree_mod, "is_debug", return_value=debug_on), \
             patch("agent_windows.uia.core._AutomationClient"), \
             patch("agent_windows.core.screenshot.get_virtual_screen_rect",
                   return_value=(0, 0, 1920, 1080)), \
             patch("agent_windows.core.screenshot.get_dpi_scale",
                   return_value=1.0):
            with caplog.at_level(logging.INFO, logger=tree_mod.logger.name):
                return traverse_tree(
                    max_depth=1, include_invisible=False, include_offscreen=False,
                    max_elements=0, timeout=0.0,
                )

    def test_profile_line_emitted_when_debug_on(self, caplog):
        self._traverse_debug(True, caplog)
        profile_lines = [r for r in caplog.records
                         if "snapshot profile:" in r.getMessage()]
        assert len(profile_lines) == 1
        msg = profile_lines[0].getMessage()
        assert "enumerate_ms=" in msg
        assert "traverse_ms=" in msg
        assert "windows=0" in msg  # enumerate_windows patched to []
        assert "max_depth=1" in msg  # configured cap, not reached depth

    def test_no_profile_line_when_debug_off(self, caplog):
        self._traverse_debug(False, caplog)
        profile_lines = [r for r in caplog.records
                         if "snapshot profile:" in r.getMessage()]
        assert profile_lines == []  # production hot path: zero profiling output


# ── Rich element metadata (TODO #15) ────────────────────────────────


class TestRichMetadata:
    """_collect_rich_metadata fills ElementInfo.metadata with toggle/value/range/
    selection/shortcut/help attributes based on control type. Drives the real
    _traverse_node against FakeElement with extra_props set."""

    def _one(self, element):
        """Traverse a single root element; return its interactive ElementInfo."""
        root = FakeElement(control_type=PANE, children=[element])
        client = FakeClient(root)
        _, interactive = traverse(client, root)
        assert len(interactive) == 1, f"expected 1 interactive, got {len(interactive)}"
        return interactive[0]

    def test_checkbox_toggle_on(self):
        el = self._one(FakeElement(
            name="Accept", control_type=CHECKBOX,
            extra_props={PropertyId.ToggleToggleStateProperty: ToggleState.On}))
        assert el.metadata["toggle_state"] == "on"

    def test_checkbox_toggle_off(self):
        el = self._one(FakeElement(
            name="Reject", control_type=CHECKBOX,
            extra_props={PropertyId.ToggleToggleStateProperty: ToggleState.Off}))
        assert el.metadata["toggle_state"] == "off"

    def test_button_no_toggle_state_omits_key(self):
        """A button that doesn't expose ToggleState (most buttons) has no
        toggle_state key — not toggle_state: None."""
        el = self._one(FakeElement(name="OK", control_type=BUTTON))
        assert "toggle_state" not in el.metadata

    def test_edit_value_and_password(self):
        el = self._one(FakeElement(
            name="Pass", control_type=EDIT,
            extra_props={PropertyId.ValueValueProperty: "secret",
                         PropertyId.IsPasswordProperty: True}))
        assert el.metadata["value"] == "secret"
        assert el.metadata["is_password"] is True

    def test_edit_empty_value_shows_placeholder(self):
        """An empty-string value surfaces as '(empty)' so the agent can tell a
        blank field from one whose value wasn't exposed."""
        el = self._one(FakeElement(
            name="User", control_type=EDIT,
            extra_props={PropertyId.ValueValueProperty: ""}))
        assert el.metadata["value"] == "(empty)"

    def test_edit_value_falls_back_to_legacy_accessible(self):
        """ValueValue absent → LegacyIAccessibleValue used (some controls only
        expose the latter)."""
        el = self._one(FakeElement(
            name="X", control_type=EDIT,
            extra_props={PropertyId.LegacyIAccessibleValueProperty: "legacy"}))
        assert el.metadata["value"] == "legacy"

    def test_combo_expand_collapse(self):
        el = self._one(FakeElement(
            name="Drop", control_type=COMBO,
            extra_props={
                PropertyId.ExpandCollapseExpandCollapseStateProperty:
                    ExpandCollapseState.Expanded}))
        assert el.metadata["expand_collapse_state"] == "expanded"

    def test_expand_collapse_on_group_outside_classic_set(self):
        """expand_collapse_state is collected for ANY element exposing a real
        expandable state — browsers expose it on GroupControl/Tree nodes
        beyond the classic ComboBox/TreeItem set. (TODO #12)"""
        GROUP = ControlType.GroupControl  # outside the classic expand set
        el = self._one(FakeElement(
            name="Section", control_type=GROUP,
            extra_props={
                PropertyId.ExpandCollapseExpandCollapseStateProperty:
                    ExpandCollapseState.Collapsed}))
        assert el.metadata["expand_collapse_state"] == "collapsed"

    def test_expand_collapse_leaf_node_omits_key(self):
        """LeafNode means 'no children to expand' — not a useful state, so the
        key is omitted to keep the tree quiet. (TODO #12)"""
        el = self._one(FakeElement(
            name="Dead", control_type=COMBO,
            extra_props={
                PropertyId.ExpandCollapseExpandCollapseStateProperty:
                    ExpandCollapseState.LeafNode}))
        assert "expand_collapse_state" not in el.metadata

    def test_scrollable_vertical(self):
        """A vertically-scrollable container reports scrollable='v' plus its
        scroll position. (TODO #12)"""
        el = self._one(FakeElement(
            name="Doc", control_type=PANE,
            extra_props={
                PropertyId.ScrollVerticallyScrollableProperty: True,
                PropertyId.ScrollHorizontallyScrollableProperty: False,
                PropertyId.ScrollVerticalScrollPercentProperty: 42.6}))
        assert el.metadata["scrollable"] == "v"
        assert el.metadata["scroll_pct"] == "v:43%"

    def test_scrollable_both_axes(self):
        """Both-axes scrollable reports 'vh' and both percentages. (TODO #12)"""
        el = self._one(FakeElement(
            name="Grid", control_type=PANE,
            extra_props={
                PropertyId.ScrollVerticallyScrollableProperty: True,
                PropertyId.ScrollHorizontallyScrollableProperty: True,
                PropertyId.ScrollVerticalScrollPercentProperty: 0.0,
                PropertyId.ScrollHorizontalScrollPercentProperty: 100.0}))
        assert el.metadata["scrollable"] == "vh"
        assert el.metadata["scroll_pct"] == "v:0% h:100%"

    def test_not_scrollable_omits_keys(self):
        """An interactive-but-non-scrollable control (a combo box with no
        scroll properties) stays quiet — no scrollable/scroll_pct keys.
        (TODO #12)"""
        el = self._one(FakeElement(name="Drop", control_type=COMBO))
        assert "scrollable" not in el.metadata
        assert "scroll_pct" not in el.metadata

    def test_scrollable_false_omits_keys(self):
        """Scroll property explicitly False (not None) also omits the keys —
        only True scrollability is reported. Tested on an interactive combo so
        it still surfaces via _one. (TODO #12)"""
        el = self._one(FakeElement(
            name="Fixed", control_type=COMBO,
            extra_props={
                PropertyId.ScrollVerticallyScrollableProperty: False,
                PropertyId.ScrollHorizontallyScrollableProperty: False}))
        assert "scrollable" not in el.metadata
        assert "scroll_pct" not in el.metadata

    def test_slider_range(self):
        """SliderControl is now interactive (TODO #15 added it), so a slider's
        range metadata surfaces via the normal traversal path."""
        el = self._one(FakeElement(
            name="Vol", control_type=SLIDER,
            extra_props={
                PropertyId.RangeValueValueProperty: 50.0,
                PropertyId.RangeValueMinimumProperty: 0.0,
                PropertyId.RangeValueMaximumProperty: 100.0}))
        assert el.metadata["value"] == 50.0
        assert el.metadata["min"] == 0.0
        assert el.metadata["max"] == 100.0

    def test_list_item_selected(self):
        el = self._one(FakeElement(
            name="file.txt", control_type=LIST_ITEM,
            extra_props={PropertyId.SelectionItemIsSelectedProperty: True}))
        assert el.metadata["is_selected"] is True

    def test_accelerator_and_help_text(self):
        """Shortcut (AcceleratorKey) and help_text apply to any control."""
        el = self._one(FakeElement(
            name="Save", control_type=BUTTON,
            extra_props={PropertyId.AcceleratorKeyProperty: "Ctrl+S",
                         PropertyId.HelpTextProperty: "Saves the document"}))
        assert el.metadata["shortcut"] == "Ctrl+S"
        assert el.metadata["help_text"] == "Saves the document"

    def test_plain_button_has_empty_metadata(self):
        """A button with no extra attributes has an empty metadata dict — keeps
        ElementInfo small and the render output quiet for plain controls."""
        el = self._one(FakeElement(name="Click", control_type=BUTTON))
        assert el.metadata == {}

    def test_metadata_rendered_in_state_column(self):
        """render_snapshot surfaces metadata keys in the State column (TODO #15),
        skipping the _orig_idx display-filtering internal."""
        from agent_windows.core.tree import render_snapshot
        el = ElementInfo(name="Check", control_type="CheckBoxControl",
                         bbox=BoundingBox(0, 0, 10, 10), is_interactive=True,
                         metadata={"toggle_state": "on", "_orig_idx": 5})
        rendered = render_snapshot({
            "cursor": (0, 0), "resolution": (100, 100), "displays": 1,
            "interactive_elements": [el],
        })
        assert "toggle_state:on" in rendered
        assert "_orig_idx" not in rendered  # internal, not user-facing

    def test_long_metadata_value_truncated_in_render(self):
        """A long value (e.g. a paragraph in an edit field) is truncated so it
        doesn't blow out the table row width."""
        from agent_windows.core.tree import render_snapshot
        long_val = "x" * 100
        el = ElementInfo(name="Notes", control_type="EditControl",
                         bbox=BoundingBox(0, 0, 10, 10), is_interactive=True,
                         metadata={"value": long_val})
        rendered = render_snapshot({
            "cursor": (0, 0), "resolution": (100, 100), "displays": 1,
            "interactive_elements": [el],
        })
        assert "..." in rendered
        assert long_val not in rendered

    def test_sentinel_not_misread_as_truthy(self):
        """A UIA NotSupported sentinel (a truthy non-None COM object) must NOT be
        misread as is_selected:true or is_password:true. _read_cached filters
        sentinels at the read boundary so bool()/str() never see them. (TODO #15)"""
        class _Sentinel:
            # Stand-in for ReservedNotSupportedValue: truthy, non-primitive.
            def __bool__(self):
                return True
        el = self._one(FakeElement(
            name="x", control_type=LIST_ITEM,
            extra_props={PropertyId.SelectionItemIsSelectedProperty: _Sentinel()}))
        assert "is_selected" not in el.metadata  # not silently True
        # Same defense for the password flag on an EditControl.
        el2 = self._one(FakeElement(
            name="y", control_type=EDIT,
            extra_props={PropertyId.IsPasswordProperty: _Sentinel()}))
        assert "is_password" not in el2.metadata

    def test_value_with_pipe_and_newline_escaped_in_render(self):
        """A value containing | or newlines must not break the markdown table row —
        pipes are escaped and newlines collapsed to spaces. (TODO #15)"""
        from agent_windows.core.tree import render_snapshot
        el = ElementInfo(name="X", control_type="EditControl",
                         bbox=BoundingBox(0, 0, 10, 10), is_interactive=True,
                         metadata={"value": "a|b\nc\rd"})
        rendered = render_snapshot({
            "cursor": (0, 0), "resolution": (100, 100), "displays": 1,
            "interactive_elements": [el],
        })
        # The rendered row stays on one line (no raw newline in output).
        table_lines = [ln for ln in rendered.splitlines() if "value:" in ln]
        assert len(table_lines) == 1
        # Pipe is backslash-escaped; newline/CR became spaces.
        assert "a\\|b c d" in table_lines[0]

    def test_pipe_near_truncation_boundary_not_split(self):
        """Truncation happens BEFORE escaping, so a pipe near the 27-char
        boundary can't be split from its backslash (which would leave a dangling
        backslash). The escaped form stays intact after truncation. (TODO #15)"""
        from agent_windows.core.tree import render_snapshot
        # 26 chars then a pipe then enough to exceed the 30-char truncation:
        # 'abcdefghijklmnopqrstuvwxyz|plus-more'
        el = ElementInfo(name="X", control_type="EditControl",
                         bbox=BoundingBox(0, 0, 10, 10), is_interactive=True,
                         metadata={"value": "abcdefghijklmnopqrstuvwxyz|plus-more"})
        rendered = render_snapshot({
            "cursor": (0, 0), "resolution": (100, 100), "displays": 1,
            "interactive_elements": [el],
        })
        # Truncated to first 27 chars ('abcdefghijklmnopqrstuvwxyz|') then
        # escaped: the pipe becomes \| and the ellipsis follows cleanly.
        assert "value:abcdefghijklmnopqrstuvwxyz\\|..." in rendered
        assert "\\..." not in rendered  # no dangling-backslash form

    def test_toggle_indeterminate_mapped(self):
        """All ToggleState values map (not just on/off) — pin indeterminate too."""
        el = self._one(FakeElement(
            name="Tri", control_type=CHECKBOX,
            extra_props={PropertyId.ToggleToggleStateProperty: ToggleState.Indeterminate}))
        assert el.metadata["toggle_state"] == "indeterminate"

    def test_radio_button_selected(self):
        """RadioButtonControl now surfaces is_selected (TODO #15 added it to the
        selection set) so the agent can tell which radio is checked."""
        RADIO = getattr(ControlType, "RadioButtonControl")
        el = self._one(FakeElement(
            name="Option A", control_type=RADIO,
            extra_props={PropertyId.SelectionItemIsSelectedProperty: True}))
        assert el.metadata["is_selected"] is True


# ── element action hints (TODO #5) ──────────────────────────────────


class TestActionFor:
    """_action_for maps control_type (+ metadata) to the action verb(s) an
    element supports. Pure function — no COM, no traversal — so each branch is
    tested directly. Mirrors the reference _ACTION_MAP but keyed to this
    project's XxxControl names."""

    def test_fill_for_text_inputs(self):
        from agent_windows.core.tree import _action_for
        assert _action_for("EditControl") == "fill"
        assert _action_for("TextBoxControl") == "fill"

    def test_toggle_for_checkbox(self):
        from agent_windows.core.tree import _action_for
        assert _action_for("CheckBoxControl") == "toggle"

    def test_select_for_choice_items(self):
        from agent_windows.core.tree import _action_for
        for ct in ("ComboBoxControl", "RadioButtonControl",
                   "ListItemControl", "TreeItemControl", "TabItemControl"):
            assert _action_for(ct) == "select", ct

    def test_slide_for_range_controls(self):
        """Slider/Spinner/ScrollBar all expose RangeValuePattern, so they share
        the slide verb (drag the thumb) rather than falling to click."""
        from agent_windows.core.tree import _action_for
        assert _action_for("SliderControl") == "slide"
        assert _action_for("SpinnerControl") == "slide"
        assert _action_for("ScrollBarControl") == "slide"

    def test_scroll_for_document(self):
        from agent_windows.core.tree import _action_for
        assert _action_for("DocumentControl") == "scroll"

    def test_click_fallback_for_unmapped_types(self):
        """Anything not in the map (Button, MenuItem, Hyperlink, Pane, …)
        defaults to click — the universal action."""
        from agent_windows.core.tree import _action_for
        for ct in ("ButtonControl", "MenuItemControl", "HyperlinkControl",
                   "PaneControl", "", "SomeUnknownControl"):
            assert _action_for(ct) == "click", ct

    def test_scrollable_metadata_overlays_scroll(self):
        """A promoted scrollable Pane isn't in _ACTION_MAP, so its ONLY action
        is the property-driven scroll — no misleading 'click' prefix, since you
        scroll a pane, you don't click it."""
        from agent_windows.core.tree import _action_for
        assert _action_for("PaneControl", {"scrollable": "v"}) == "scroll"

    def test_expandable_metadata_overlays_expand(self):
        """A promoted expandable Group gets expand via metadata, not control
        type — its only action is expand."""
        from agent_windows.core.tree import _action_for
        assert _action_for("GroupControl",
                           {"expand_collapse_state": "collapsed"}) == "expand"

    def test_scrollable_document_gets_single_scroll(self):
        """DocumentControl already maps to scroll; a scrollable metadata key
        must NOT duplicate it into scroll/scroll."""
        from agent_windows.core.tree import _action_for
        assert _action_for("DocumentControl", {"scrollable": "v"}) == "scroll"

    def test_combo_with_expand_state_is_select_expand(self):
        """A ComboBox is select by type; when it also exposes
        expand_collapse_state (most do), expand is appended → select/expand."""
        from agent_windows.core.tree import _action_for
        assert _action_for("ComboBoxControl",
                           {"expand_collapse_state": "collapsed"}) == "select/expand"

    def test_scrollable_and_expandable_yields_two_verbs(self):
        """A container that both scrolls and expands (e.g. a scrollable
        expandable Group) gets scroll/expand — both real actions, no phantom
        click (there's nothing to click)."""
        from agent_windows.core.tree import _action_for
        assert _action_for("GroupControl",
                           {"scrollable": "v",
                            "expand_collapse_state": "expanded"}) == "scroll/expand"

    def test_metadata_without_relevant_keys_uses_type_only(self):
        """toggle_state/value/is_selected don't change the verb — only
        scrollable/expand_collapse_state do. A CheckBox with toggle_state stays
        just 'toggle' (no phantom expand/scroll)."""
        from agent_windows.core.tree import _action_for
        assert _action_for("CheckBoxControl",
                           {"toggle_state": "on"}) == "toggle"


class TestActionColumnRender:
    """render_snapshot emits an Action column between Type and Name with the
    verb(s) from _action_for."""

    def _render(self, elements):
        from agent_windows.core.tree import render_snapshot
        return render_snapshot({
            "cursor": (0, 0), "resolution": (100, 100), "displays": 1,
            "interactive_elements": elements,
        })

    def test_action_column_header_present(self):
        rendered = self._render([ElementInfo(
            name="OK", control_type="ButtonControl",
            bbox=BoundingBox(0, 0, 10, 10), is_interactive=True)])
        assert "| Ref | Type | Action | Name |" in rendered

    def test_button_row_shows_click(self):
        rendered = self._render([ElementInfo(
            name="OK", control_type="ButtonControl",
            bbox=BoundingBox(0, 0, 10, 10), is_interactive=True)])
        row = [ln for ln in rendered.splitlines() if "@e1" in ln][0]
        assert "| click |" in row

    def test_combo_with_expand_shows_select_expand(self):
        rendered = self._render([ElementInfo(
            name="Font", control_type="ComboBoxControl",
            bbox=BoundingBox(0, 0, 10, 10), is_interactive=True,
            metadata={"expand_collapse_state": "collapsed"})])
        row = [ln for ln in rendered.splitlines() if "@e1" in ln][0]
        assert "| select/expand |" in row

    def test_promoted_scrollable_pane_shows_scroll(self):
        """A scrollable Pane promoted to interactive gets just 'scroll' — proving
        the metadata overlay reaches the rendered column, not just the helper."""
        rendered = self._render([ElementInfo(
            name="Content", control_type="PaneControl",
            bbox=BoundingBox(0, 0, 10, 10), is_interactive=True,
            metadata={"scrollable": "v", "scroll_pct": "v:42%"})])
        row = [ln for ln in rendered.splitlines() if "@e1" in ln][0]
        assert "| scroll |" in row
        # The State column is untouched — scroll_pct still surfaces there too.
        assert "scroll_pct:v:42%" in row


# ── empty-container pruning (TODO #6) ──────────────────────────────

# Structural control types not predefined as shorthand constants above.
GROUP = getattr(ControlType, "GroupControl")
TOOLBAR = getattr(ControlType, "ToolBarControl")
TAB = getattr(ControlType, "TabControl")


class TestEmptyContainerPruning:
    """_traverse_node prunes unnamed structural containers whose whole subtree
    produced nothing useful (no interactive element, no named leaf) — they're
    pure layout noise. Named containers are kept. Interactive collection is
    independent of the prune decision so @eN refs stay stable."""

    def test_unnamed_empty_pane_pruned_from_tree(self):
        """A nameless Pane with no useful descendants is dropped — it carries
        zero information for the agent."""
        root = FakeElement(control_type=PANE, children=[
            FakeElement(control_type=PANE),  # unnamed, empty → pruned
            FakeElement(name="OK", control_type=BUTTON),
        ])
        _link_siblings(root.children)
        lines, _ = traverse(FakeClient(root), root)
        joined = "\n".join(lines)
        # The empty Pane leaves no line; the button still renders.
        assert 'Button "OK"' in joined
        # Exactly one Pane line remains — the unnamed empty one is gone. (The
        # root Pane is depth 0 and never emits its own line, so the only Pane
        # line that COULD appear is the pruned child — assert it doesn't.) Match
        # by substring: the line is prefixed with a tree connector ("├─ Pane …").
        pane_lines = [ln for ln in lines if "Pane" in ln]
        assert pane_lines == []

    def test_named_empty_pane_kept(self):
        """A Pane WITH a name is kept even when empty — it may label a region
        (matches the reference's 'promote named structural containers' rule)."""
        root = FakeElement(control_type=PANE, children=[
            FakeElement(name="Content region", control_type=PANE),  # named, empty → kept
        ])
        _link_siblings(root.children)
        lines, _ = traverse(FakeClient(root), root)
        joined = "\n".join(lines)
        assert 'Pane "Content region"' in joined

    def test_pane_with_interactive_child_kept(self):
        """A Pane whose subtree has an interactive element is kept (it groups
        that element) — pruning never loses useful descendants."""
        root = FakeElement(control_type=PANE, children=[
            FakeElement(control_type=PANE, children=[  # unnamed but has interactive child → kept
                FakeElement(name="Submit", control_type=BUTTON),
            ]),
        ])
        _link_siblings_and_grandchildren(root)
        lines, interactive = traverse(FakeClient(root), root)
        joined = "\n".join(lines)
        # Both the inner Pane (grouping) and its button render.
        assert "Pane" in joined
        assert 'Button "Submit"' in joined
        # And the button is still collected (prune didn't touch interactive list).
        assert [e.name for e in interactive] == ["Submit"]

    def test_nested_empty_containers_all_pruned(self):
        """Empty containers nested inside empty containers collapse entirely —
        pruning is recursive bottom-up (mirrors reference _prune_structural)."""
        root = FakeElement(control_type=PANE, children=[
            FakeElement(control_type=PANE, children=[  # unnamed, only has empty child → pruned
                FakeElement(control_type=GROUP),  # unnamed, empty → pruned
            ]),
        ])
        _link_siblings_and_grandchildren(root)
        lines, _ = traverse(FakeClient(root), root)
        # No Pane or Group lines survive — the whole empty chain is gone. Match
        # by substring: lines are prefixed with a tree connector ("├─ Pane …").
        structural_lines = [ln for ln in lines
                            if "Pane" in ln or "Group" in ln]
        assert structural_lines == []

    def test_all_structural_types_pruned_when_unnamed_empty(self):
        """Every type in _STRUCTURAL_TYPES is pruned the same way when unnamed
        and empty — not just Pane."""
        from agent_windows.core.tree import _STRUCTURAL_TYPES
        # Build a control-type id for each structural name via getattr.
        ct_ids = {getattr(ControlType, ct): ct for ct in _STRUCTURAL_TYPES}
        for ct_id, ct_name in ct_ids.items():
            root = FakeElement(control_type=PANE, children=[
                FakeElement(control_type=ct_id),  # unnamed, empty structural
            ])
            _link_siblings(root.children)
            lines, _ = traverse(FakeClient(root), root)
            label = ct_name.replace("Control", "")
            # Match by substring: the line is prefixed with a tree connector
            # ("├─ Pane …"), so startswith would miss it.
            struct_lines = [ln for ln in lines if label in ln]
            assert struct_lines == [], f"{ct_name} should be pruned when unnamed+empty"

    def test_interactive_collected_even_when_parent_line_pruned(self):
        """Interactive collection is independent of tree-line pruning: a truly
        pruned container (unnamed empty) contributes nothing to `interactive`,
        and any interactive element under a KEPT container is collected. Uses a
        container kept only because it has an interactive descendant — the
        invariant the prune logic relies on (a subtree with an interactive
        element is, by construction, useful and unpruned)."""
        root = FakeElement(control_type=PANE, children=[
            FakeElement(control_type=GROUP, children=[
                FakeElement(name="OK", control_type=BUTTON),
            ]),
        ])
        _link_siblings_and_grandchildren(root)
        _, interactive = traverse(FakeClient(root), root)
        assert [e.name for e in interactive] == ["OK"]

    def test_promoted_scrollable_childless_pane_line_kept(self):
        """A structural Pane PROMOTED to interactive by metadata (scrollable)
        is kept even when childless and nameless — otherwise the tree would hide
        an actionable target the Interactive Elements table still references via
        @eN. This is the ``not is_interactive`` guard on the prune rule (review
        finding E)."""
        root = FakeElement(control_type=PANE, children=[
            FakeElement(control_type=PANE, extra_props={  # nameless, no children
                PropertyId.ScrollVerticallyScrollableProperty: True,
                PropertyId.ScrollHorizontallyScrollableProperty: False,
                PropertyId.ScrollVerticalScrollPercentProperty: 0.0,
            }),
        ])
        _link_siblings(root.children)
        lines, interactive = traverse(FakeClient(root), root)
        joined = "\n".join(lines)
        # The childless scrollable Pane is BOTH in the tree (a line survives)...
        pane_lines = [ln for ln in lines if "Pane" in ln]
        assert len(pane_lines) == 1, f"expected scrollable pane line kept, got {pane_lines}"
        assert "[interactive]" in pane_lines[0]
        # ...AND in the interactive list (collected regardless of prune).
        assert len(interactive) == 1
        assert interactive[0].metadata.get("scrollable") == "v"

    def test_non_structural_node_with_name_always_emitted(self):
        """A non-structural node (e.g. Text) is never pruned — it's emitted
        whenever it has a name, even with no descendants."""
        root = FakeElement(control_type=PANE, children=[
            FakeElement(name="Hello world", control_type=TEXT),
        ])
        _link_siblings(root.children)
        lines, _ = traverse(FakeClient(root), root)
        joined = "\n".join(lines)
        assert 'Text "Hello world"' in joined

    def test_traverse_node_returns_useful_bool(self):
        """_traverse_node returns True when the subtree produced useful content,
        False when it was pruned to nothing — the signal parents prune on."""
        win = WindowInfo(title="W", process_name="p", handle=1,
                         bbox=BoundingBox(0, 0, 10, 10))
        client = FakeClient(FakeElement(control_type=PANE))
        cache_request = tree._build_traversal_cache(client)
        # Subtree with an interactive button → useful.
        useful_node = FakeElement(control_type=GROUP, children=[
            FakeElement(name="Go", control_type=BUTTON)])
        _link_siblings(useful_node.children)
        assert tree._traverse_node(client, useful_node, [], [], win,
                                   15, False, False, 4, 1, time.time(), 10.0,
                                   0, float("inf"), cache_request) is True
        # Empty unnamed structural → not useful.
        empty_node = FakeElement(control_type=GROUP)
        assert tree._traverse_node(client, empty_node, [], [], win,
                                   15, False, False, 4, 1, time.time(), 10.0,
                                   0, float("inf"), cache_request) is False


def _link_siblings_and_grandchildren(root):
    """Link children of the root AND children of each root child (two levels),
    for nesting tests. _link_siblings only does one level."""
    _link_siblings(root.children)
    for child in root.children:
        if child.children:
            _link_siblings(child.children)


# ── resolve_window: snapshot --window / --active selector (TODO #16) ──


def _win(title="App", process="app.exe", handle=1, active=False):
    """Build a WindowInfo with keyword args (robust against future field
    additions); mirrors the keyword style used elsewhere in this file."""
    return WindowInfo(
        title=title,
        process_name=process,
        handle=handle,
        bbox=BoundingBox(0, 0, 100, 100),
        is_active=active,
        class_name="",
        status="",
        is_browser=False,
    )


class TestResolveWindow:
    """resolve_window resolves a top-level window for snapshot --window /
    --active. Each test patches enumerate_windows so no Win32 / desktop access
    happens — the function is a pure filter over a WindowInfo list."""

    @patch("agent_windows.core.tree.enumerate_windows")
    def test_title_substring_first_match(self, mock_enum):
        mock_enum.return_value = [_win(title="Notepad"),
                                  _win(title="Calculator", handle=2)]
        win, warn = resolve_window(value="note")
        assert win.title == "Notepad"
        assert warn is None

    @patch("agent_windows.core.tree.enumerate_windows")
    def test_title_case_insensitive(self, mock_enum):
        mock_enum.return_value = [_win(title="Notepad")]
        win, _ = resolve_window(value="NOTEPAD")
        assert win.title == "Notepad"

    @patch("agent_windows.core.tree.enumerate_windows")
    def test_process_fallback(self, mock_enum):
        mock_enum.return_value = [_win(title="Untitled", process="notepad.exe")]
        win, warn = resolve_window(value="notepad")
        assert win.process_name == "notepad.exe"
        assert warn is None

    @patch("agent_windows.core.tree.enumerate_windows")
    def test_hwnd_decimal(self, mock_enum):
        mock_enum.return_value = [_win(handle=12345, title="A")]
        win, _ = resolve_window(value="12345")
        assert win.handle == 12345

    @patch("agent_windows.core.tree.enumerate_windows")
    def test_hwnd_hex(self, mock_enum):
        mock_enum.return_value = [_win(handle=0x1a2b, title="A")]
        win, _ = resolve_window(value="0x1a2b")
        assert win.handle == 0x1a2b

    @patch("agent_windows.core.tree.enumerate_windows")
    def test_active_picks_foreground(self, mock_enum):
        mock_enum.return_value = [_win(title="BG", handle=1, active=False),
                                  _win(title="FG", handle=2, active=True)]
        win, warn = resolve_window(active=True)
        assert win.title == "FG"
        assert warn is None

    @patch("agent_windows.core.tree.enumerate_windows")
    def test_active_no_foreground_raises(self, mock_enum):
        mock_enum.return_value = [_win(title="BG", active=False)]
        with pytest.raises(ValueError, match="No active foreground window"):
            resolve_window(active=True)

    @patch("agent_windows.core.tree.enumerate_windows")
    def test_multiple_title_matches_warning(self, mock_enum):
        mock_enum.return_value = [_win(title="calc — main", handle=1),
                                  _win(title="calc — options", handle=2)]
        win, warn = resolve_window(value="calc")
        assert win.title == "calc — main"
        assert warn is not None
        assert "2 windows" in warn
        assert "calc — main" in warn
        assert "HWND" in warn

    @patch("agent_windows.core.tree.enumerate_windows")
    def test_multiple_process_matches_warning(self, mock_enum):
        mock_enum.return_value = [_win(title="A", process="win.exe", handle=1),
                                  _win(title="B", process="win.exe", handle=2)]
        win, warn = resolve_window(value="win")
        assert warn is not None and "2 windows" in warn

    @patch("agent_windows.core.tree.enumerate_windows")
    def test_no_match_raises(self, mock_enum):
        mock_enum.return_value = [_win(title="Notepad")]
        with pytest.raises(ValueError, match="Window not found"):
            resolve_window(value="nonexistent")

    @patch("agent_windows.core.tree.enumerate_windows")
    def test_hwnd_no_match_raises(self, mock_enum):
        mock_enum.return_value = [_win(handle=999)]
        with pytest.raises(ValueError, match="Window not found"):
            resolve_window(value="12345")


class TestTraverseTreeOnlyWindow:
    """traverse_tree(only_window=...) skips enumerate_windows and scopes the
    traversal to a single target window. Patches the UIA / Win32 entry points
    so no real desktop access happens (mirrors TestTraverseTreeMetadata)."""

    @patch("agent_windows.core.tree._traverse_window")
    @patch("agent_windows.core.tree._build_traversal_cache", return_value=object())
    @patch("agent_windows.uia.core._AutomationClient")
    @patch("agent_windows.core.screenshot.get_dpi_scale", return_value=1.0)
    @patch("agent_windows.core.screenshot.get_virtual_screen_rect",
           return_value=(0, 0, 1920, 1080))
    @patch("agent_windows.core.tree.enumerate_windows")
    def test_only_window_skips_enumerate(self, mock_enum, m_vs, m_dpi, m_client,
                                         m_cache, m_trav):
        target = _win(title="Only", handle=42)
        data = traverse_tree(only_window=target)
        mock_enum.assert_not_called()
        assert data["windows"] == [target]
        assert data["active_window"] is target

    @patch("agent_windows.core.tree._traverse_window")
    @patch("agent_windows.core.tree._build_traversal_cache", return_value=object())
    @patch("agent_windows.uia.core._AutomationClient")
    @patch("agent_windows.core.screenshot.get_dpi_scale", return_value=1.0)
    @patch("agent_windows.core.screenshot.get_virtual_screen_rect",
           return_value=(0, 0, 1920, 1080))
    @patch("agent_windows.core.tree.enumerate_windows", return_value=[])
    def test_none_window_keeps_enumerate(self, mock_enum, m_vs, m_dpi, m_client,
                                         m_cache, m_trav):
        traverse_tree()
        mock_enum.assert_called_once()


# ── snapshot-diff (core) ─────────────────────────────────────────────


def _el(name="", ctype="ButtonControl", window="App", bbox=None, **kw):
    """Tiny ElementInfo builder for diff tests."""
    from agent_windows.core.tree import ElementInfo, BoundingBox
    if bbox is None:
        bbox = BoundingBox(0, 0, 10, 10)
    elif isinstance(bbox, tuple):
        bbox = BoundingBox(*bbox)
    return ElementInfo(name=name, control_type=ctype, bbox=bbox,
                       window_name=window, **kw)


def test_element_info_window_handle_field_default_and_kwarg():
    # New stable per-element window anchor. Default 0 keeps existing constructors
    # and the _el fixture backward-compatible.
    from agent_windows.core.tree import ElementInfo, BoundingBox
    el = ElementInfo(name="X", control_type="ButtonControl",
                     bbox=BoundingBox(0, 0, 1, 1))
    assert el.window_handle == 0
    el2 = ElementInfo(name="X", control_type="ButtonControl",
                      bbox=BoundingBox(0, 0, 1, 1), window_handle=4242)
    assert el2.window_handle == 4242


def test_diff_pure_added_removed():
    from agent_windows.core.tree import diff_snapshots
    base = {"interactive_elements": [_el("A", bbox=(0, 0, 10, 10))],
            "windows": []}
    # B is far outside MATCH_RADIUS so the two do NOT pair → genuine add+remove
    # (a co-located B would pair with A and surface as a name *change*, which
    # test_diff_name_change_is_changed_not_add_remove covers instead).
    cur = {"interactive_elements": [_el("B", bbox=(500, 500, 510, 510))],
           "windows": []}
    d = diff_snapshots(base, cur)
    assert len(d["added"]) == 1 and d["added"][0].name == "B"
    assert len(d["removed"]) == 1 and d["removed"][0].name == "A"
    assert d["changed"] == []


def test_diff_name_change_is_changed_not_add_remove():
    from agent_windows.core.tree import diff_snapshots
    base = {"interactive_elements": [_el("Loading", bbox=(0, 0, 10, 10))],
            "windows": []}
    cur = {"interactive_elements": [_el("Done", bbox=(0, 0, 10, 10))],
           "windows": []}
    d = diff_snapshots(base, cur)
    assert d["added"] == [] and d["removed"] == []
    assert len(d["changed"]) == 1
    fields = {f for f, _, _ in d["changed"][0]["deltas"]}
    assert "name" in fields


def test_diff_same_bucket_bbox_pairing():
    from agent_windows.core.tree import diff_snapshots, BoundingBox
    base = {"interactive_elements": [
        _el("OK", bbox=BoundingBox(0, 0, 10, 10)),
        _el("OK", bbox=BoundingBox(100, 100, 110, 110)),
    ], "windows": []}
    cur = {"interactive_elements": [
        _el("OK", bbox=BoundingBox(2, 2, 12, 12)),
        _el("OK", bbox=BoundingBox(102, 102, 112, 112)),
    ], "windows": []}
    d = diff_snapshots(base, cur)
    assert d["added"] == [] and d["removed"] == [] and d["changed"] == []


def test_diff_cross_window_same_type_different_handle_no_mispair():
    # Two windows, each a Button at the SAME bbox, different handles. They must
    # NOT pair (different identity buckets). control_type-only identity would
    # put them in one bucket and pair them by the coincident bbox — masking a
    # real add/remove. window_handle is the precision anchor that prevents this.
    from agent_windows.core.tree import diff_snapshots
    base = {"interactive_elements": [
        _el("OK", ctype="ButtonControl", window="App A", window_handle=1,
            bbox=(0, 0, 10, 10)),
    ], "windows": []}
    cur = {"interactive_elements": [
        _el("OK", ctype="ButtonControl", window="App B", window_handle=2,
            bbox=(0, 0, 10, 10)),  # same bbox + type, different window
    ], "windows": []}
    d = diff_snapshots(base, cur)
    assert len(d["removed"]) == 1 and len(d["added"]) == 1
    assert d["changed"] == []


def test_diff_match_radius_boundary():
    from agent_windows.core.tree import (
        diff_snapshots, _SNAPSHOT_DIFF_MATCH_RADIUS, BoundingBox,
    )
    # Centres on the X axis so distance == abs(dx). Just-AT-the-radius pairs
    # (skip condition is strict `dist > radius`, so dist == radius pairs).
    r = _SNAPSHOT_DIFF_MATCH_RADIUS
    base = {"interactive_elements": [_el("X", bbox=BoundingBox(0, 0, 10, 10))],
            "windows": []}
    # current centre at 5+r → centre distance == r → pairs (NOT removed+added).
    # The pair surfaces as `changed:moved` (60px > MOVE_THRESHOLD), but that is
    # move-detection, not the pairing boundary under test — assert only what the
    # radius controls: pairing keeps both sides out of removed/added.
    cur_in = {"interactive_elements": [
        _el("X", bbox=BoundingBox(int(5 + r) - 5, 0, int(5 + r) + 5, 10))],
        "windows": []}
    d = diff_snapshots(base, cur_in)
    assert d["added"] == [] and d["removed"] == []
    # one pixel beyond → no pair → removed+added
    cur_out = {"interactive_elements": [
        _el("X", bbox=BoundingBox(int(5 + r + 1) - 5, 0, int(5 + r + 1) + 5, 10))],
        "windows": []}
    d = diff_snapshots(base, cur_out)
    assert len(d["removed"]) == 1 and len(d["added"]) == 1


def test_diff_changed_fields_and_move():
    from agent_windows.core.tree import diff_snapshots, BoundingBox
    base_el = _el("Search", ctype="EditControl", bbox=BoundingBox(0, 0, 100, 20),
                  is_enabled=False)
    base_el.metadata["value"] = ""
    cur_el = _el("Search", ctype="EditControl", bbox=BoundingBox(20, 0, 120, 20),
                 is_enabled=True)
    cur_el.metadata["value"] = "hello"
    d = diff_snapshots(
        {"interactive_elements": [base_el], "windows": []},
        {"interactive_elements": [cur_el], "windows": []})
    assert len(d["changed"]) == 1
    fields = {f for f, _, _ in d["changed"][0]["deltas"]}
    assert {"enabled", "value", "moved"} <= fields


def test_diff_windows_add_remove():
    from agent_windows.core.tree import diff_snapshots, WindowInfo, BoundingBox
    def w(t, p, h): return WindowInfo(t, p, h, BoundingBox(0, 0, 1, 1))
    d = diff_snapshots(
        {"interactive_elements": [],
         "windows": [w("A", "a.exe", 1), w("B", "b.exe", 2)]},
        {"interactive_elements": [],
         "windows": [w("B", "b.exe", 2), w("C", "c.exe", 3)]})
    assert [x.title for x in d["windows_added"]] == ["C"]
    assert [x.title for x in d["windows_removed"]] == ["A"]


def test_diff_window_title_change_reports_windows_changed_not_per_element():
    # A window-title change (load done, tab switch) shifts every element's
    # window_name at once. It must NOT (a) flood Added/Removed (identity is
    # control_type-only, so elements still pair) nor (b) flood element Changed
    # with per-element changed:window rows. It reports ONCE at window level as
    # windows_changed.
    from agent_windows.core.tree import diff_snapshots, WindowInfo, BoundingBox
    def win(t): return WindowInfo(t, "app.exe", 4242, BoundingBox(0, 0, 1, 1))
    base = {"interactive_elements": [
        _el("Submit", ctype="ButtonControl", window="Login", bbox=(10, 10, 60, 40)),
        _el("", ctype="EditControl", window="Login", bbox=(10, 50, 200, 70)),
    ], "windows": [win("Login")]}
    cur = {"interactive_elements": [
        _el("Submit", ctype="ButtonControl", window="Login - Success",
            bbox=(10, 10, 60, 40)),
        _el("", ctype="EditControl", window="Login - Success",
            bbox=(10, 50, 200, 70)),
    ], "windows": [win("Login - Success")]}
    d = diff_snapshots(base, cur)
    assert d["added"] == [] and d["removed"] == [] and d["changed"] == []
    assert d["windows_added"] == [] and d["windows_removed"] == []
    assert len(d["windows_changed"]) == 1
    old_t, new_t = d["windows_changed"][0]["title"]
    assert old_t == "Login" and new_t == "Login - Success"


def test_diff_window_title_unchanged_no_windows_changed():
    # Same title, same handle -> no windows_changed entry.
    from agent_windows.core.tree import diff_snapshots, WindowInfo, BoundingBox
    def win(t): return WindowInfo(t, "app.exe", 4242, BoundingBox(0, 0, 1, 1))
    d = diff_snapshots(
        {"interactive_elements": [], "windows": [win("App")]},
        {"interactive_elements": [], "windows": [win("App")]})
    assert d["windows_changed"] == []


def test_diff_windows_title_change_pairs_by_handle():
    # Windows identity is (process_name, handle) — stable across a title change.
    from agent_windows.core.tree import diff_snapshots, WindowInfo, BoundingBox
    def w(t, p, h): return WindowInfo(t, p, h, BoundingBox(0, 0, 1, 1))
    d = diff_snapshots(
        {"interactive_elements": [], "windows": [w("Loading", "app.exe", 4242)]},
        {"interactive_elements": [], "windows": [w("Ready", "app.exe", 4242)]})
    assert d["windows_added"] == [] and d["windows_removed"] == []
    assert len(d["windows_changed"]) == 1  # same window, title changed
    d2 = diff_snapshots(
        {"interactive_elements": [], "windows": [w("A", "app.exe", 4242)]},
        {"interactive_elements": [], "windows": [w("A", "app.exe", 8888)]})
    assert len(d2["windows_added"]) == 1 and len(d2["windows_removed"]) == 1


def test_diff_identical_is_empty():
    from agent_windows.core.tree import diff_snapshots
    d = diff_snapshots({"interactive_elements": [_el("A", bbox=(0, 0, 10, 10))],
                        "windows": []},
                       {"interactive_elements": [_el("A", bbox=(0, 0, 10, 10))],
                        "windows": []})
    assert d["added"] == [] and d["removed"] == [] and d["changed"] == []


def test_render_diff_multiline_value_is_single_lined():
    # A multiline value (e.g. a Notepad document's full text) must render on ONE
    # line — newlines in the value would otherwise split the Changed row across
    # lines and scramble the section. The new tail ("789") stays visible.
    from agent_windows.core.tree import diff_snapshots, render_snapshot_diff
    base_el = _el("Doc", ctype="EditControl", bbox=(0, 0, 10, 10))
    base_el.metadata["value"] = "你好 Hello\n123\n456"
    cur_el = _el("Doc", ctype="EditControl", bbox=(0, 0, 10, 10))
    cur_el.metadata["value"] = "你好 Hello\n123\n456\n789"
    cur = {"interactive_elements": [cur_el], "windows": []}
    d = diff_snapshots({"interactive_elements": [base_el], "windows": []}, cur)
    out = render_snapshot_diff(d, cur)
    value_lines = [ln for ln in out.splitlines() if "value:" in ln]
    assert value_lines, "no value delta rendered"
    assert "789" in value_lines[0]     # delta on one line, new tail visible
    assert "\n" not in value_lines[0]  # no embedded newline


def test_render_snapshot_diff_markdown_and_current_refs():
    from agent_windows.core.tree import diff_snapshots, render_snapshot_diff
    base = {"interactive_elements": [_el("OK", bbox=(0, 0, 10, 10))], "windows": []}
    cur_els = [_el("Submit", bbox=(0, 0, 10, 10)), _el("OK", bbox=(0, 0, 10, 10))]
    cur = {"interactive_elements": cur_els, "windows": []}
    d = diff_snapshots(base, cur)
    out = render_snapshot_diff(d, cur)
    assert "## Snapshot Diff" in out
    assert "Added" in out
    # One current element landed in Added, another in Changed; both carry a
    # current @eN ref. Invariant: a ref appears in each non-empty section.
    import re
    assert re.search(r"@e\d", out)
    assert "Changed" in out
    # Stronger: each non-empty section must carry at least one @eN ref (a naive
    # render change that dropped refs from one section but not the other would
    # still satisfy the single whole-output search above).
    def section(heading):
        m = re.search(rf"### {heading}.*?(?=\n### |\Z)", out, re.S)
        return m.group(0) if m else ""
    assert re.search(r"@e\d", section("Added")), "Added section missing @eN"
    assert re.search(r"@e\d", section("Changed")), "Changed section missing @eN"


def test_render_snapshot_diff_shows_scope_in_header():
    from agent_windows.core.tree import diff_snapshots, render_snapshot_diff
    base = {"interactive_elements": [_el("A", bbox=(0, 0, 10, 10))], "windows": []}
    cur = {"interactive_elements": [_el("A", bbox=(0, 0, 10, 10))], "windows": []}
    d = diff_snapshots(base, cur)
    # Scope is injected by the Task 2 handler before render; core never sets it.
    # The handler's _scope_label quotes window titles, so the value carries
    # its own quotes — render just interpolates into `(scope: {scope})`.
    d["stats"]["scope"] = '"Login"'
    out = render_snapshot_diff(d, cur)
    assert '(scope: "Login")' in out
    # Without scope, no parenthetical appears, but the element-count line does.
    del d["stats"]["scope"]
    out_noscope = render_snapshot_diff(d, cur)
    assert "(scope:" not in out_noscope
    assert "elements → current:" in out_noscope


def test_render_snapshot_diff_uses_orig_idx_refs():
    from agent_windows.core.tree import diff_snapshots, render_snapshot_diff
    base = {"interactive_elements": [], "windows": []}
    # Display-filtered current element: _orig_idx stashes its ORIGINAL 1-based
    # index in the unfiltered snapshot. render_snapshot_diff must emit @e7 so
    # the ref still resolves via resolve_element (which numbers the unfiltered
    # list), NOT @e1 from a naive 1-based enumerate over the filtered current.
    cur_el = _el("Search", bbox=(0, 0, 10, 10))
    cur_el.metadata["_orig_idx"] = 7
    cur = {"interactive_elements": [cur_el], "windows": []}
    d = diff_snapshots(base, cur)
    out = render_snapshot_diff(d, cur)
    assert "@e7" in out
    assert "@e1" not in out




