"""Unit tests for core.pixel — deterministic screen color sampling.

Uses synthetic PIL/numpy images with ``source=path`` for every test, so the
suite never touches a real screen capture and runs on any platform. The
daemon/CLI/MCP layers are exercised in their own tests.
"""

from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from agent_windows.core.pixel import (
    MAX_SAMPLE_SIDE,
    _parse_color,
    pixel_at,
)


def _save(arr: np.ndarray, path: Path) -> str:
    """Save an (H,W,3) uint8 array as a PNG and return its string path."""
    Image.fromarray(arr.astype("uint8"), "RGB").save(path)
    return str(path)


def _solid(w: int, h: int, color: tuple[int, int, int]) -> np.ndarray:
    return np.full((h, w, 3), color, dtype="uint8")


# ── _parse_color ────────────────────────────────────────────────────


class TestParseColor:
    def test_hash_six(self):
        assert _parse_color("#dc1414") == (220, 20, 20)

    def test_hash_three_expands_each_digit(self):
        # #abc → aa,bb,cc (each nibble repeated to form an 8-bit channel)
        assert _parse_color("#abc") == (0xAA, 0xBB, 0xCC)

    def test_rgb_paren_commas(self):
        assert _parse_color("rgb(1, 2, 3)") == (1, 2, 3)

    def test_rgb_paren_spaces(self):
        assert _parse_color("rgb(1 2 3)") == (1, 2, 3)

    def test_bare_six(self):
        assert _parse_color("ff0000") == (255, 0, 0)

    def test_whitespace_and_case_normalized(self):
        assert _parse_color(" #AABBCC ") == (0xAA, 0xBB, 0xCC)

    @pytest.mark.parametrize(
        "bad",
        [
            "#xyz",          # non-hex chars
            "#12345",        # wrong length after #
            "#",             # bare hash
            "rgb(1,2)",      # too few channels
            "rgb(1,2,300)",  # channel out of 0-255
            "rgb(1, 2, 3",   # missing closing paren
            "12",            # too short bare
            "12345",         # 5 chars (ambiguous)
            "",              # empty
            "hello",         # garbage
        ],
    )
    def test_invalid_raises_value_error(self, bad):
        with pytest.raises(ValueError):
            _parse_color(bad)

    def test_non_string_raises(self):
        with pytest.raises(ValueError):
            _parse_color(None)  # type: ignore[arg-type]


# ── read pixel color ────────────────────────────────────────────────


class TestReadColor:
    def test_solid_color_center(self, tmp_path):
        src = _save(_solid(10, 10, (220, 20, 20)), tmp_path / "src.png")
        r = pixel_at(5, 5, source=src)
        assert r["rgb"] == [220, 20, 20]
        assert r["hex"] == "#dc1414"
        assert r["x"] == 5 and r["y"] == 5
        assert r["sample"] == 1
        assert r["method"] == "pixel"
        assert r["screenshot_path"] == ""
        assert "match" not in r

    def test_sample_box_averages_to_pure_color(self, tmp_path):
        # 5x5 block of pure red in a black field; a 5x5 sample at the block's
        # center (7, 7) — box [5,10) x [5,10) — averages to (255, 0, 0).
        arr = _solid(20, 20, (0, 0, 0))
        arr[5:10, 5:10] = (255, 0, 0)
        src = _save(arr, tmp_path / "src.png")
        r = pixel_at(7, 7, source=src, sample=5)
        assert r["rgb"] == [255, 0, 0]

    @pytest.mark.parametrize("n", [2, 4])
    def test_even_sample_returns_valid_color(self, tmp_path, n):
        # Even N is accepted: the (sample-1)//2 half formula biases one pixel
        # toward +x/+y, but a 2x2 / 4x4 box still averages to a valid color
        # and must not crash. Locks the formula against a regression to
        # sample//2 (which would shift the box by a pixel and still work on a
        # solid source — the assertion is "doesn't crash, sample echoed").
        arr = _solid(20, 20, (10, 20, 30))
        src = _save(arr, tmp_path / "src.png")
        r = pixel_at(10, 10, source=src, sample=n)
        assert r["rgb"] == [10, 20, 30]
        assert r["sample"] == n

    def test_sample_larger_than_image_clamps_to_whole_image(self, tmp_path):
        # A sample larger than the source must clamp to the whole image and
        # succeed rather than raise — covers the whole-image clamp path. The
        # motivating case is a tiny source icon with a generous --sample.
        arr = _solid(5, 5, (40, 50, 60))
        src = _save(arr, tmp_path / "src.png")
        r = pixel_at(2, 2, source=src, sample=101)
        assert r["rgb"] == [40, 50, 60]
        assert r["sample"] == 101


# ── match ───────────────────────────────────────────────────────────


class TestMatch:
    def test_exact_match_score_is_one(self, tmp_path):
        src = _save(_solid(10, 10, (100, 0, 0)), tmp_path / "src.png")
        r = pixel_at(5, 5, source=src, match="#640000")  # 100, 0, 0
        assert r["match"]["is_match"] is True
        assert r["match"]["max_diff"] == 0
        # Strict == so a mean-vs-max formula regression (which approx() would
        # tolerate) surfaces here.
        assert r["match"]["score"] == 1.0
        assert r["match"]["tolerance"] == 0

    def test_match_true_at_boundary_diff_equals_tolerance(self, tmp_path):
        # Region (100, 0, 0); target (110, 0, 0) → max_diff = 10. With
        # tolerance = 10, the boundary is inclusive → is_match True.
        src = _save(_solid(10, 10, (100, 0, 0)), tmp_path / "src.png")
        r = pixel_at(5, 5, source=src, match="#6e0000", tolerance=10)  # 110,0,0
        assert r["match"]["is_match"] is True
        assert r["match"]["max_diff"] == 10
        assert r["match"]["score"] == round(1.0 - 10 / 255.0, 4)

    def test_match_false_large_diff(self, tmp_path):
        src = _save(_solid(10, 10, (0, 0, 0)), tmp_path / "src.png")
        r = pixel_at(5, 5, source=src, match="#ffffff", tolerance=10)
        assert r["match"]["is_match"] is False
        assert r["match"]["max_diff"] == 255
        assert r["match"]["score"] == 0.0

    def test_match_target_normalized_lowercase_hex(self, tmp_path):
        src = _save(_solid(10, 10, (0, 0, 0)), tmp_path / "src.png")
        r = pixel_at(5, 5, source=src, match="#AABBCC")
        assert r["match"]["target"] == "#aabbcc"


# ── bounds & clamping ──────────────────────────────────────────────


class TestBounds:
    def test_oob_file_coord_raises(self, tmp_path):
        src = _save(_solid(5, 5, (0, 0, 0)), tmp_path / "src.png")
        with pytest.raises(ValueError, match="outside"):
            pixel_at(10, 10, source=src)
        with pytest.raises(ValueError, match="outside"):
            pixel_at(-1, 0, source=src)
        with pytest.raises(ValueError, match="outside"):
            pixel_at(0, 99, source=src)

    def test_corner_sample_clamps_without_error(self, tmp_path):
        # 5x5 sample centered at (0, 0): pre-clamp box spans (-2,-2)-(3,3).
        # Must clamp to the 3x3 region actually in-source and average those
        # pixels rather than raise — a corner LED on the taskbar edge is the
        # motivating case (find-image I-1 lesson).
        arr = _solid(10, 10, (40, 80, 120))
        src = _save(arr, tmp_path / "src.png")
        r = pixel_at(0, 0, source=src, sample=5)
        assert r["rgb"] == [40, 80, 120]


# ── param validation ───────────────────────────────────────────────


class TestParamValidation:
    def test_invalid_sample(self, tmp_path):
        src = _save(_solid(5, 5, (0, 0, 0)), tmp_path / "src.png")
        with pytest.raises(ValueError):
            pixel_at(1, 1, source=src, sample=0)
        # Spec calls out 0 and 102; MAX_SAMPLE_SIDE = 101 → 102 is one over.
        assert MAX_SAMPLE_SIDE == 101
        with pytest.raises(ValueError):
            pixel_at(1, 1, source=src, sample=102)

    def test_max_sample_side_accepted(self, tmp_path):
        # MAX_SAMPLE_SIDE (101) is accepted on a source large enough to fit
        # the 101x101 box. Symmetric to the 102-rejected test above — locks
        # the inclusive upper bound.
        arr = _solid(200, 200, (7, 8, 9))
        src = _save(arr, tmp_path / "src.png")
        r = pixel_at(100, 100, source=src, sample=MAX_SAMPLE_SIDE)
        assert r["rgb"] == [7, 8, 9]
        assert r["sample"] == MAX_SAMPLE_SIDE

    def test_invalid_tolerance(self, tmp_path):
        src = _save(_solid(5, 5, (0, 0, 0)), tmp_path / "src.png")
        with pytest.raises(ValueError):
            pixel_at(1, 1, source=src, tolerance=-1)
        with pytest.raises(ValueError):
            pixel_at(1, 1, source=src, tolerance=256)

    def test_float_coord_rejected(self, tmp_path):
        # Non-integral float must not be silently floored (find-image M-1).
        src = _save(_solid(5, 5, (0, 0, 0)), tmp_path / "src.png")
        with pytest.raises(ValueError):
            pixel_at(1.5, 1, source=src)  # type: ignore[arg-type]
        # Even an integral float is rejected: the contract is `x: int`.
        with pytest.raises(ValueError):
            pixel_at(1, 1.0, source=src)  # type: ignore[arg-type]


# ── grayscale match ────────────────────────────────────────────────


class TestGrayscaleMatch:
    def test_color_shifted_matches_in_luma_only(self, tmp_path):
        # Region solid gray (50, 50, 50), luma ≈ 50.
        # Target (0, 100, 0): very different in RGB, but PIL's L conversion
        # gives luma ≈ 59 → diff ≈ 9, well inside tolerance=20.
        src = _save(_solid(10, 10, (50, 50, 50)), tmp_path / "src.png")
        rgb_res = pixel_at(5, 5, source=src, match="#006400", tolerance=20)
        assert rgb_res["match"]["is_match"] is False  # max RGB diff = 50
        gray_res = pixel_at(
            5, 5, source=src, match="#006400", tolerance=20, grayscale=True,
        )
        assert gray_res["match"]["is_match"] is True

    def test_grayscale_rgb_is_luma_replicated(self, tmp_path):
        src = _save(_solid(10, 10, (50, 50, 50)), tmp_path / "src.png")
        r = pixel_at(5, 5, source=src, grayscale=True)
        # Reported rgb is the luma replicated across all three channels.
        assert r["rgb"][0] == r["rgb"][1] == r["rgb"][2]

    def test_grayscale_match_target_is_gray_equivalent(self, tmp_path):
        # In grayscale mode the target must be reported as its luma-equivalent
        # gray so target/rgb/is_match read consistently (otherwise a result
        # like {target: #006400, rgb: [50,50,50], is_match: true} looks wrong).
        # #006400 (0,100,0) has PIL Luma = 59 = 0x3b → #3b3b3b. Locks Fix 2.
        src = _save(_solid(10, 10, (50, 50, 50)), tmp_path / "src.png")
        r = pixel_at(
            5, 5, source=src, match="#006400", grayscale=True, tolerance=20,
        )
        target = r["match"]["target"]
        # All three octets equal — gray form, not the user's original color.
        assert target[1:3] == target[3:5] == target[5:7]
        assert target == "#3b3b3b"


# ── output annotation ──────────────────────────────────────────────


class TestOutput:
    def test_output_saves_annotated_image_and_sets_path(self, tmp_path):
        src = _save(_solid(10, 10, (220, 20, 20)), tmp_path / "src.png")
        out = tmp_path / "out.png"
        r = pixel_at(5, 5, source=src, output=str(out))
        assert r["screenshot_path"] == str(out)
        assert Path(r["screenshot_path"]).exists()

    def test_output_context_window_is_at_least_annotate_context(self, tmp_path):
        # Fix 1: with --output the annotation canvas is a context window of
        # side >= ANNOTATE_CONTEXT (64), not the 1x1 sample box. On a source
        # large enough to fit it, the saved image is exactly 64x64 for
        # sample=1.
        from agent_windows.core.pixel import ANNOTATE_CONTEXT

        assert ANNOTATE_CONTEXT == 64
        arr = _solid(200, 200, (220, 20, 20))
        src = _save(arr, tmp_path / "src.png")
        out = tmp_path / "out.png"
        r = pixel_at(100, 100, source=src, sample=1, output=str(out))
        assert r["screenshot_path"] == str(out)
        with Image.open(out) as im:
            assert im.size == (ANNOTATE_CONTEXT, ANNOTATE_CONTEXT)

    def test_output_context_window_clamps_to_small_source(self, tmp_path):
        # When the source is smaller than ANNOTATE_CONTEXT, the context window
        # clamps to the whole source (no error, no padding).
        src = _save(_solid(10, 10, (220, 20, 20)), tmp_path / "src.png")
        out = tmp_path / "out.png"
        r = pixel_at(5, 5, source=src, output=str(out))
        assert r["screenshot_path"] == str(out)
        with Image.open(out) as im:
            assert im.size == (10, 10)

    def test_no_output_means_empty_screenshot_path(self, tmp_path):
        src = _save(_solid(10, 10, (220, 20, 20)), tmp_path / "src.png")
        r = pixel_at(5, 5, source=src)
        assert r["screenshot_path"] == ""
