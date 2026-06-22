"""Tests for daemon/handlers/multi.py: MultiSelect and MultiEdit (TODO #12).

The core multi_select/multi_edit are patched out (covered by
test_input_controller.py); these tests pin the handler plumbing: param
extraction, locs/selectors parsing + validation, coordinate bounds, error
propagation, and the count cap.
"""

import pytest
from unittest.mock import patch

from agent_windows.daemon.router import HANDLERS, CoordinateRangeError


_VSCREEN = (0, 0, 1920, 1080)


@pytest.fixture(autouse=True)
def _mock_vscreen(monkeypatch):
    """Pin the virtual screen so bounds checks are deterministic."""
    monkeypatch.setattr(
        "agent_windows.core.input_controller._get_virtual_screen",
        lambda: _VSCREEN,
    )


# ── multi_select ─────────────────────────────────────────────────────


class TestMultiSelectHandler:
    def test_locs_forwarded_with_ctrl_default(self):
        """Default press_ctrl=True; locs validated and forwarded as tuples."""
        with patch("agent_windows.core.input_controller.multi_select") as m:
            r = HANDLERS["multi_select"]({"locs": [[10, 20], [30, 40]]})
        m.assert_called_once_with(True, [(10, 20), (30, 40)])
        assert r["count"] == 2
        assert r["press_ctrl"] is True
        assert "(10,20)" in r["output"]

    def test_press_ctrl_false_forwarded(self):
        with patch("agent_windows.core.input_controller.multi_select") as m:
            HANDLERS["multi_select"]({"locs": [[1, 2]], "press_ctrl": False})
        m.assert_called_once_with(False, [(1, 2)])

    def test_selectors_resolved_to_centers(self):
        with patch("agent_windows.core.tree.resolve_element_center",
                   side_effect=[(100, 200), (300, 400)]) as m_res, \
                patch("agent_windows.core.input_controller.multi_select") as m_ms:
            r = HANDLERS["multi_select"]({"selectors": ["@e1", "@e3"]})
        assert m_res.call_args_list[0].args[0] == "@e1"
        assert m_res.call_args_list[1].args[0] == "@e3"
        m_ms.assert_called_once_with(True, [(100, 200), (300, 400)])
        assert r["count"] == 2

    def test_neither_locs_nor_selectors_raises(self):
        with pytest.raises(ValueError, match="provide either locs or selectors"):
            HANDLERS["multi_select"]({})

    def test_both_locs_and_selectors_raises(self):
        with pytest.raises(ValueError, match="not both"):
            HANDLERS["multi_select"]({"locs": [[1, 2]], "selectors": ["@e1"]})

    def test_bad_loc_entry_shape_raises(self):
        with pytest.raises(ValueError, match=r"locs\[0\] must be a \[x, y\] pair"):
            HANDLERS["multi_select"]({"locs": [[1, 2, 3]]})

    def test_out_of_bounds_coord_raises(self):
        with pytest.raises(CoordinateRangeError):
            HANDLERS["multi_select"]({"locs": [[99999, 99999]]})

    def test_too_many_targets_rejected_before_resolution(self):
        """The 50-target cap fires BEFORE selector resolution so a huge selector
        list can't trigger up to N UIA traversals before being rejected (DoS bound)."""
        # locs path
        with patch("agent_windows.core.input_controller.multi_select"):
            with pytest.raises(ValueError, match="too many targets"):
                HANDLERS["multi_select"]({"locs": [[i, i] for i in range(51)]})
        # selectors path: resolve_element_center must NOT be called at all
        with patch("agent_windows.core.tree.resolve_element_center") as m_res:
            with pytest.raises(ValueError, match="too many targets"):
                HANDLERS["multi_select"](
                    {"selectors": [f"@e{i}" for i in range(51)]})
        m_res.assert_not_called()

    def test_selector_error_names_index(self):
        """A bad selector surfaces with its index so the caller knows which one failed."""
        with patch("agent_windows.core.tree.resolve_element_center",
                   side_effect=ValueError("Invalid selector: 'bad'")):
            with pytest.raises(ValueError, match=r"selectors\[0\]"):
                HANDLERS["multi_select"]({"selectors": ["bad"]})

    def test_offscreen_selector_preserves_coordinate_error(self):
        """A CoordinateRangeError from resolve_element_center must NOT be rewrapped
        as a plain ValueError — the router maps the former to COORDINATE_OUT_OF_RANGE
        (-1002), the latter to INVALID_PARAMS (-32602). Pin the preservation."""
        with patch("agent_windows.core.tree.resolve_element_center",
                   side_effect=CoordinateRangeError("off screen")):
            with pytest.raises(CoordinateRangeError):
                HANDLERS["multi_select"]({"selectors": ["@e1"]})

    def test_empty_locs_rejected(self):
        with pytest.raises(ValueError, match="non-empty"):
            HANDLERS["multi_select"]({"locs": []})

    def test_press_ctrl_must_be_bool(self):
        with pytest.raises(ValueError, match="press_ctrl must be a boolean"):
            HANDLERS["multi_select"]({"locs": [[1, 2]], "press_ctrl": "yes"})


# ── multi_edit ───────────────────────────────────────────────────────


class TestMultiEditHandler:
    def test_locs_forwarded_as_triples(self):
        with patch("agent_windows.core.input_controller.multi_edit") as m:
            r = HANDLERS["multi_edit"]({"locs": [[10, 20, "Alice"], [30, 40, "Bob"]]})
        m.assert_called_once_with([(10, 20, "Alice"), (30, 40, "Bob")])
        assert r["count"] == 2
        assert "Alice" in r["output"]

    def test_selectors_resolved_with_text(self):
        with patch("agent_windows.core.tree.resolve_element_center",
                   return_value=(100, 200)) as m_res, \
                patch("agent_windows.core.input_controller.multi_edit") as m_me:
            HANDLERS["multi_edit"]({"selectors": [["@e1", "Alice"], ["@e3", "Bob"]]})
        assert m_res.call_count == 2
        m_me.assert_called_once_with([(100, 200, "Alice"), (100, 200, "Bob")])

    def test_bad_triple_shape_raises(self):
        with pytest.raises(ValueError, match=r"locs\[0\] must be a \[x, y, text\]"):
            HANDLERS["multi_edit"]({"locs": [[1, 2]]})

    def test_non_string_text_rejected(self):
        with pytest.raises(ValueError, match=r"locs\[0\] text must be a string"):
            HANDLERS["multi_edit"]({"locs": [[1, 2, 123]]})

    def test_neither_arg_raises(self):
        with pytest.raises(ValueError, match="provide either locs or selectors"):
            HANDLERS["multi_edit"]({})

    def test_both_args_raises(self):
        with pytest.raises(ValueError, match="not both"):
            HANDLERS["multi_edit"]({"locs": [[1, 2, "a"]], "selectors": [["@e1", "a"]]})

    def test_too_many_targets_rejected_before_resolution(self):
        """Cap fires BEFORE selector resolution (same DoS rationale as multi_select)."""
        with patch("agent_windows.core.input_controller.multi_edit"):
            with pytest.raises(ValueError, match="too many targets"):
                HANDLERS["multi_edit"]({"locs": [[i, i, "x"] for i in range(51)]})
        with patch("agent_windows.core.tree.resolve_element_center") as m_res:
            with pytest.raises(ValueError, match="too many targets"):
                HANDLERS["multi_edit"](
                    {"selectors": [[f"@e{i}", "x"] for i in range(51)]})
        m_res.assert_not_called()

    def test_oversized_text_rejected_locs(self):
        """Each text field is capped (same DoS bound as the type handler)."""
        with pytest.raises(ValueError, match="10000-character maximum"):
            HANDLERS["multi_edit"]({"locs": [[1, 2, "x" * 10001]]})

    def test_oversized_text_rejected_selectors(self):
        """The text cap also applies on the selectors path (parallel coverage)."""
        with pytest.raises(ValueError, match="10000-character maximum"):
            HANDLERS["multi_edit"]({"selectors": [["@e1", "x" * 10001]]})

    def test_selector_error_names_index(self):
        """A bad selector on multi_edit surfaces with its index (parallel to select)."""
        with patch("agent_windows.core.tree.resolve_element_center",
                   side_effect=ValueError("Invalid selector: 'bad'")):
            with pytest.raises(ValueError, match=r"selectors\[0\]"):
                HANDLERS["multi_edit"]({"selectors": [["bad", "x"]]})

    def test_offscreen_selector_preserves_coordinate_error(self):
        """CoordinateRangeError preserved on the multi_edit selectors path too."""
        with patch("agent_windows.core.tree.resolve_element_center",
                   side_effect=CoordinateRangeError("off screen")):
            with pytest.raises(CoordinateRangeError):
                HANDLERS["multi_edit"]({"selectors": [["@e1", "x"]]})

    def test_out_of_bounds_coord_raises_locs(self):
        """Locs-path coordinate bounds also surface as CoordinateRangeError (NOT
        downgraded to INVALID_PARAMS) — symmetric with the selectors path."""
        with pytest.raises(CoordinateRangeError):
            HANDLERS["multi_edit"]({"locs": [[99999, 99999, "x"]]})
