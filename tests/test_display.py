"""Tests for multi-display filtering (TODO #9-B).

Pure-function tests for core/display.py — no COM, no daemon. GetMonitorsRect
is patched where needed. Snapshot dicts are hand-built from WindowInfo /
ElementInfo / BoundingBox so filter_snapshot_to_displays is driven directly.
"""

from unittest.mock import patch

import pytest

from agent_windows.core.display import (
    parse_display_selection,
    get_display_union_bbox,
    clip_bbox_to_region,
    point_in_region,
    filter_snapshot_to_displays,
)
from agent_windows.core.tree import BoundingBox, ElementInfo, WindowInfo
from agent_windows.uia.core import Rect


# ── parse_display_selection ─────────────────────────────────────────


class TestParseDisplaySelection:
    def test_none_returns_none(self):
        assert parse_display_selection(None) is None

    def test_empty_string_returns_none(self):
        assert parse_display_selection("") is None

    def test_int_wraps_to_list(self):
        assert parse_display_selection(0) == [0]

    def test_list_dedups_preserving_order(self):
        assert parse_display_selection([0, 1, 1, 0, 2]) == [0, 1, 2]

    def test_tuple_accepted(self):
        assert parse_display_selection((1, 2)) == [1, 2]

    def test_empty_list_returns_none(self):
        assert parse_display_selection([]) is None

    def test_bool_rejected(self):
        """bool is an int subclass — True must NOT silently become display 1."""
        with pytest.raises(ValueError, match="non-negative integers"):
            parse_display_selection(True)

    def test_bool_element_rejected(self):
        with pytest.raises(ValueError, match="non-negative integers"):
            parse_display_selection([0, True])

    def test_negative_rejected(self):
        with pytest.raises(ValueError, match="non-negative integers"):
            parse_display_selection([-1])

    def test_non_int_rejected(self):
        with pytest.raises(ValueError, match="non-negative integers"):
            parse_display_selection([0, "1"])

    def test_wrong_type_rejected(self):
        with pytest.raises(ValueError, match="JSON array"):
            parse_display_selection("0")


# ── get_display_union_bbox ──────────────────────────────────────────


class TestGetDisplayUnionBbox:
    def _patch(self, rects):
        return patch("agent_windows.uia.core.GetMonitorsRect",
                     return_value=[Rect(l, t, r, b) for (l, t, r, b) in rects])

    def test_single_monitor(self):
        with self._patch([(0, 0, 1920, 1080)]):
            box = get_display_union_bbox([0])
        assert box == BoundingBox(0, 0, 1920, 1080)

    def test_two_monitors_union(self):
        with self._patch([(0, 0, 1920, 1080), (1920, 0, 3840, 1080)]):
            box = get_display_union_bbox([0, 1])
        assert box == BoundingBox(0, 0, 3840, 1080)

    def test_select_one_of_two(self):
        with self._patch([(0, 0, 1920, 1080), (1920, 0, 3840, 1080)]):
            box = get_display_union_bbox([1])
        assert box == BoundingBox(1920, 0, 3840, 1080)

    def test_negative_origin_monitor(self):
        """Secondary monitor left of primary → union has negative left."""
        with self._patch([(-1920, 0, 0, 1080), (0, 0, 1920, 1080)]):
            box = get_display_union_bbox([0])
        assert box == BoundingBox(-1920, 0, 0, 1080)

    def test_out_of_range_index_raises(self):
        with self._patch([(0, 0, 1920, 1080)]):
            with pytest.raises(ValueError, match="Invalid display index"):
                get_display_union_bbox([5])

    def test_empty_monitor_list_raises(self):
        with self._patch([]):
            with pytest.raises(ValueError, match="No displays detected"):
                get_display_union_bbox([0])


# ── clip_bbox_to_region ─────────────────────────────────────────────


class TestClipBboxToRegion:
    def test_inside_returned_unchanged(self):
        region = BoundingBox(0, 0, 1000, 1000)
        box = BoundingBox(100, 100, 200, 200)
        assert clip_bbox_to_region(box, region) == box

    def test_partially_outside_clipped(self):
        region = BoundingBox(0, 0, 1000, 1000)
        box = BoundingBox(-100, 500, 200, 1500)
        assert clip_bbox_to_region(box, region) == BoundingBox(0, 500, 200, 1000)

    def test_no_overlap_returns_none(self):
        region = BoundingBox(0, 0, 1000, 1000)
        box = BoundingBox(2000, 2000, 3000, 3000)
        assert clip_bbox_to_region(box, region) is None

    def test_edge_touch_returns_none(self):
        """Box edge exactly on region edge → zero-width/height overlap → None."""
        region = BoundingBox(0, 0, 1000, 1000)
        box = BoundingBox(1000, 0, 2000, 500)  # left == region.right
        assert clip_bbox_to_region(box, region) is None

    def test_none_box_returns_none(self):
        assert clip_bbox_to_region(None, BoundingBox(0, 0, 10, 10)) is None


# ── point_in_region ─────────────────────────────────────────────────


class TestPointInRegion:
    def test_inside(self):
        assert point_in_region((50, 50), BoundingBox(0, 0, 100, 100)) is True

    def test_outside(self):
        assert point_in_region((150, 50), BoundingBox(0, 0, 100, 100)) is False

    def test_half_open_top_left(self):
        """Region is half-open: the top-left corner is inside."""
        assert point_in_region((0, 0), BoundingBox(0, 0, 100, 100)) is True

    def test_half_open_bottom_right(self):
        """Region is half-open: the bottom-right edge is OUTSIDE."""
        assert point_in_region((100, 100), BoundingBox(0, 0, 100, 100)) is False

    def test_none_point(self):
        assert point_in_region(None, BoundingBox(0, 0, 100, 100)) is False


# ── filter_snapshot_to_displays ─────────────────────────────────────


def _snapshot_data(windows=None, elements=None, active=None, cursor=(500, 500)):
    return {
        "cursor": cursor,
        "resolution": (1920, 1080),
        "virtual_screen": (0, 0, 1920, 1080),
        "dpi_scale": 1.0,
        "displays": 2,
        "active_window": active,
        "windows": windows or [],
        "semantic_tree": "Desktop\n  └─ Window",
        "interactive_elements": elements or [],
    }


class TestFilterSnapshotToDisplays:
    def _patch_mon(self, rects):
        return patch("agent_windows.uia.core.GetMonitorsRect",
                     return_value=[Rect(l, t, r, b) for (l, t, r, b) in rects])

    def test_window_intersecting_kept_and_clipped(self):
        """A window straddling the region edge is kept, bbox clipped."""
        data = _snapshot_data(windows=[
            WindowInfo("App", "app", 1, BoundingBox(-100, 0, 500, 800)),
        ])
        with self._patch_mon([(0, 0, 1920, 1080)]):
            out = filter_snapshot_to_displays(data, [0])
        assert len(out["windows"]) == 1
        # clipped to region left edge (0)
        assert out["windows"][0].bbox == BoundingBox(0, 0, 500, 800)
        # other fields preserved
        assert out["windows"][0].title == "App"

    def test_window_no_overlap_dropped(self):
        data = _snapshot_data(windows=[
            WindowInfo("Off", "off", 2, BoundingBox(3000, 0, 4000, 800)),
        ])
        with self._patch_mon([(0, 0, 1920, 1080)]):
            out = filter_snapshot_to_displays(data, [0])
        assert out["windows"] == []

    def test_cross_display_window_clipped_to_selected(self):
        """A window spanning two displays, when selecting display 0, is kept but
        clipped to display 0 (intersect-clip, not 'belongs to')."""
        data = _snapshot_data(windows=[
            WindowInfo("Span", "span", 1, BoundingBox(1000, 0, 3000, 800)),
        ])
        with self._patch_mon([(0, 0, 1920, 1080), (1920, 0, 3840, 1080)]):
            out = filter_snapshot_to_displays(data, [0])
        assert len(out["windows"]) == 1
        assert out["windows"][0].bbox == BoundingBox(1000, 0, 1920, 800)

    def test_active_window_clipped_or_nulled(self):
        active = WindowInfo("Act", "act", 1, BoundingBox(0, 0, 500, 500),
                            is_active=True)
        data = _snapshot_data(active=active)
        with self._patch_mon([(0, 0, 1920, 1080)]):
            out = filter_snapshot_to_displays(data, [0])
        assert out["active_window"] is not None
        assert out["active_window"].bbox == active.bbox  # inside region, unchanged

    def test_active_window_off_display_nulled(self):
        active = WindowInfo("Act", "act", 1, BoundingBox(3000, 0, 3500, 500),
                            is_active=True)
        data = _snapshot_data(active=active)
        with self._patch_mon([(0, 0, 1920, 1080)]):
            out = filter_snapshot_to_displays(data, [0])
        assert out["active_window"] is None

    def test_elements_filtered(self):
        data = _snapshot_data(elements=[
            ElementInfo("In", "ButtonControl", BoundingBox(100, 100, 200, 200),
                        is_interactive=True),
            ElementInfo("Out", "ButtonControl",
                        BoundingBox(3000, 0, 3100, 100), is_interactive=True),
        ])
        with self._patch_mon([(0, 0, 1920, 1080)]):
            out = filter_snapshot_to_displays(data, [0])
        assert len(out["interactive_elements"]) == 1
        assert out["interactive_elements"][0].name == "In"

    def test_modal_order_preserved(self):
        """Filter only drops, never reorders — so the input order survives
        verbatim. A re-bugging sort (modals-to-front) would move Modal2 before
        Normal. Input interleaves a non-modal between two modals to make the
        two orderings distinguishable; all survive (on-display)."""
        data = _snapshot_data(elements=[
            ElementInfo("Modal", "ButtonControl",
                        BoundingBox(100, 100, 200, 200),
                        is_interactive=True, is_modal=True),
            ElementInfo("Normal", "ButtonControl",
                        BoundingBox(300, 100, 400, 200), is_interactive=True),
            ElementInfo("Modal2", "ButtonControl",
                        BoundingBox(500, 100, 600, 200),
                        is_interactive=True, is_modal=True),
        ])
        with self._patch_mon([(0, 0, 1920, 1080)]):
            out = filter_snapshot_to_displays(data, [0])
        # Exact original order — NOT sorted (which would be Modal, Modal2, Normal)
        assert [e.name for e in out["interactive_elements"]] == \
            ["Modal", "Normal", "Modal2"]

    def test_cursor_in_region_kept(self):
        data = _snapshot_data(cursor=(500, 500))
        with self._patch_mon([(0, 0, 1920, 1080)]):
            out = filter_snapshot_to_displays(data, [0])
        assert out["cursor"] == (500, 500)

    def test_cursor_off_region_nulled(self):
        data = _snapshot_data(cursor=(3000, 500))
        with self._patch_mon([(0, 0, 1920, 1080)]):
            out = filter_snapshot_to_displays(data, [0])
        assert out["cursor"] is None

    def test_display_filter_key_set(self):
        data = _snapshot_data()
        with self._patch_mon([(0, 0, 1920, 1080)]):
            out = filter_snapshot_to_displays(data, [0])
        assert out["display_filter"] == [0]

    def test_desktop_metadata_preserved(self):
        """resolution/virtual_screen/dpi_scale/displays are NOT filtered."""
        data = _snapshot_data()
        with self._patch_mon([(0, 0, 1920, 1080), (1920, 0, 3840, 1080)]):
            out = filter_snapshot_to_displays(data, [0])
        assert out["resolution"] == (1920, 1080)
        assert out["virtual_screen"] == (0, 0, 1920, 1080)
        assert out["dpi_scale"] == 1.0
        assert out["displays"] == 2

    def test_semantic_tree_unchanged(self):
        """The tree text is free text — NOT filtered (only a note is attached)."""
        tree = "Desktop\n  └─ Window"
        data = _snapshot_data()
        data["semantic_tree"] = tree
        with self._patch_mon([(0, 0, 1920, 1080)]):
            out = filter_snapshot_to_displays(data, [0])
        assert out["semantic_tree"] == tree

    def test_survivors_keep_original_index(self):
        """B-1: each surviving element keeps its ORIGINAL 1-based index in
        metadata['_orig_idx'], so render emits non-contiguous @eN refs that map
        1:1 to resolve_element's UNFILTERED list. Renumbering from 1 would
        silently point every @eN at the wrong element."""
        data = _snapshot_data(elements=[
            ElementInfo("E1", "ButtonControl",
                        BoundingBox(100, 100, 200, 200), is_interactive=True),
            ElementInfo("E2", "ButtonControl",
                        BoundingBox(3000, 0, 3100, 100), is_interactive=True),  # off-display
            ElementInfo("E3", "ButtonControl",
                        BoundingBox(300, 100, 400, 200), is_interactive=True),
        ])
        with self._patch_mon([(0, 0, 1920, 1080)]):
            out = filter_snapshot_to_displays(data, [0])
        # E2 dropped; E1 and E3 survive with their ORIGINAL indices 1 and 3
        survivors = out["interactive_elements"]
        assert [e.name for e in survivors] == ["E1", "E3"]
        assert survivors[0].metadata["_orig_idx"] == 1
        assert survivors[1].metadata["_orig_idx"] == 3  # NOT renumbered to 2

    def test_original_data_untouched(self):
        """filter returns a NEW dict; the input is not mutated."""
        win = WindowInfo("App", "app", 1, BoundingBox(-100, 0, 500, 800))
        data = _snapshot_data(windows=[win])
        original_bbox = win.bbox
        with self._patch_mon([(0, 0, 1920, 1080)]):
            filter_snapshot_to_displays(data, [0])
        assert win.bbox == original_bbox  # input window bbox unchanged
        assert "display_filter" not in data  # original dict has no such key


# ── render_snapshot integration (cursor N/A + display note) ─────────


class TestRenderDisplayFilter:
    def test_cursor_none_renders_na(self):
        from agent_windows.core.tree import render_snapshot
        rendered = render_snapshot({
            "cursor": None, "resolution": (1920, 1080), "displays": 1,
            "active_window": None, "windows": [], "semantic_tree": "",
            "interactive_elements": [],
        })
        assert "Cursor: N/A (off selected display)" in rendered

    def test_display_filter_note_rendered(self):
        from agent_windows.core.tree import render_snapshot
        rendered = render_snapshot({
            "cursor": (10, 10), "resolution": (1920, 1080), "displays": 2,
            "active_window": None, "windows": [], "semantic_tree": "Desktop",
            "interactive_elements": [], "display_filter": [0, 1],
        })
        assert "Accessibility Tree not filtered" in rendered
        assert "display(s) 0,1" in rendered

    def test_no_filter_no_note(self):
        """Without display_filter, no note appears (backward compatible)."""
        from agent_windows.core.tree import render_snapshot
        rendered = render_snapshot({
            "cursor": (10, 10), "resolution": (1920, 1080), "displays": 1,
            "active_window": None, "windows": [], "semantic_tree": "Desktop",
            "interactive_elements": [],
        })
        assert "Accessibility Tree not filtered" not in rendered
