"""Unit tests for core.image_match — deterministic template matching.

Uses synthetic numpy/PIL images (no real screenshots), so tests are fast and
platform-independent. The daemon/CLI layers are exercised in their own tests.
"""

from pathlib import Path

import numpy as np
import pytest
from PIL import Image


def _save(arr: np.ndarray, path: Path) -> str:
    """Save an (H,W,3) uint8 array as a PNG and return its string path."""
    Image.fromarray(arr.astype("uint8"), "RGB").save(path)
    return str(path)


def _solid(w: int, h: int, color: tuple[int, int, int]) -> np.ndarray:
    return np.full((h, w, 3), color, dtype="uint8")


class TestExactMatch:
    def test_finds_template_at_correct_coords(self, tmp_path):
        from agent_windows.core.image_match import find_image

        src = _solid(20, 20, (0, 0, 0))
        src[5:8, 12:15] = (255, 0, 0)                 # a 3x3 red block at (12,5)
        src_path = _save(src, tmp_path / "src.png")
        tpl = _solid(3, 3, (255, 0, 0))
        tpl_path = _save(tpl, tmp_path / "tpl.png")

        result = find_image(tpl_path, source=src_path)
        assert result["count"] == 1
        m = result["matches"][0]
        assert (m["x"], m["y"]) == (12, 5)
        assert (m["w"], m["h"]) == (3, 3)
        assert (m["center_x"], m["center_y"]) == (13, 6)
        # Exact match: 0 max-diff → round(1.0, 4) == 1.0 exactly. Use a
        # strict == so a formula regression (e.g. wrong axis reduction) that
        # approx() would tolerate surfaces here.
        assert m["score"] == 1.0
        assert result["method"] == "template"
        assert result["x"] == 13 and result["y"] == 6

    def test_no_match_returns_count_zero_not_exception(self, tmp_path):
        from agent_windows.core.image_match import find_image

        src = _solid(10, 10, (0, 0, 0))
        tpl = _solid(3, 3, (255, 255, 255))
        result = find_image(_save(tpl, tmp_path / "tpl.png"),
                            source=_save(src, tmp_path / "src.png"))
        assert result["count"] == 0
        assert result["matches"] == []
        assert "x" not in result


class TestTolerance:
    def test_tolerance_matches_imperfect_template(self, tmp_path):
        from agent_windows.core.image_match import find_image

        src = _solid(20, 20, (0, 0, 0))
        src[5:8, 5:8] = (100, 0, 0)
        src_path = _save(src, tmp_path / "src.png")
        tpl = _solid(3, 3, (110, 0, 0))
        tpl_path = _save(tpl, tmp_path / "tpl.png")

        assert find_image(tpl_path, source=src_path)["count"] == 0
        got = find_image(tpl_path, source=src_path, tolerance=10)["count"]
        assert got == 1
        res = find_image(tpl_path, source=src_path, tolerance=10)
        assert res["matches"][0]["score"] == pytest.approx(1 - 10 / 255, abs=0.01)


class TestGrayscale:
    def test_grayscale_matches_color_shifted_template(self, tmp_path):
        from agent_windows.core.image_match import find_image

        src = _solid(20, 20, (0, 0, 0))
        src[4:7, 4:7] = (50, 50, 50)
        src_path = _save(src, tmp_path / "src.png")
        tpl = _solid(3, 3, (52, 48, 50))
        tpl_path = _save(tpl, tmp_path / "tpl.png")
        assert find_image(tpl_path, source=src_path, tolerance=0)["count"] == 0
        assert find_image(tpl_path, source=src_path,
                          tolerance=2, grayscale=True)["count"] == 1


class TestAllMatchesAndNMS:
    def test_all_returns_disjoint_hits(self, tmp_path):
        from agent_windows.core.image_match import find_image

        src = _solid(40, 10, (0, 0, 0))
        src[2:5, 2:5] = (255, 0, 0)
        src[2:5, 30:33] = (255, 0, 0)
        src_path = _save(src, tmp_path / "src.png")
        tpl = _save(_solid(3, 3, (255, 0, 0)), tmp_path / "tpl.png")

        result = find_image(tpl, source=src_path, all_matches=True)
        assert result["count"] == 2
        xs = sorted(m["x"] for m in result["matches"])
        assert xs == [2, 30]
        assert "x" not in result

    def test_nms_collapses_overlapping_hits(self, tmp_path):
        from agent_windows.core.image_match import find_image

        # 5x5 block + 3x3 template → 9 pre-NMS matching positions (a 3x3 grid
        # of valid top-lefts). NMS must collapse to 1. If _nms returned its
        # input unchanged, count would be 9.
        src = _solid(20, 20, (0, 0, 0))
        src[10:15, 10:15] = (100, 0, 0)
        src_path = _save(src, tmp_path / "src.png")
        tpl = _save(_solid(3, 3, (100, 0, 0)), tmp_path / "tpl.png")
        result = find_image(tpl, source=src_path, tolerance=0, all_matches=True)
        assert result["count"] == 1


class TestRegion:
    def test_region_offsets_coords_and_speeds_search(self, tmp_path):
        from agent_windows.core.image_match import find_image

        src = _solid(40, 40, (0, 0, 0))
        src[20:23, 30:33] = (255, 0, 0)
        src_path = _save(src, tmp_path / "src.png")
        tpl = _save(_solid(3, 3, (255, 0, 0)), tmp_path / "tpl.png")
        result = find_image(tpl, source=src_path, region=(25, 15, 40, 40))
        assert result["count"] == 1
        assert (result["matches"][0]["x"], result["matches"][0]["y"]) == (30, 20)

    def test_out_of_bounds_region_raises(self, tmp_path):
        from agent_windows.core.image_match import find_image

        src = _save(_solid(10, 10, (0, 0, 0)), tmp_path / "src.png")
        tpl = _save(_solid(3, 3, (255, 0, 0)), tmp_path / "tpl.png")
        with pytest.raises(ValueError, match="outside the source"):
            find_image(tpl, source=src, region=(0, 0, 99, 99))


class TestSizeGuards:
    def test_template_larger_than_source_raises(self, tmp_path):
        from agent_windows.core.image_match import find_image

        src = _save(_solid(5, 5, (0, 0, 0)), tmp_path / "src.png")
        tpl = _save(_solid(10, 10, (255, 0, 0)), tmp_path / "tpl.png")
        with pytest.raises(ValueError, match="larger than the source"):
            find_image(tpl, source=src)

    def test_template_over_pixel_cap_raises(self, tmp_path):
        from agent_windows.core.image_match import (
            find_image, MAX_FIND_TEMPLATE_PIXELS,
        )
        side = 130                                       # 130*130 = 16900 > 16384 cap
        src = _save(_solid(200, 200, (0, 0, 0)), tmp_path / "src.png")
        tpl = _save(_solid(side, side, (255, 0, 0)), tmp_path / "tpl.png")
        with pytest.raises(ValueError, match=str(MAX_FIND_TEMPLATE_PIXELS)):
            find_image(tpl, source=src)
