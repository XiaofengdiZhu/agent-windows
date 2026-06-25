"""Unit tests for utils/formatting.py — the format_size helper.

Pure-function tests with no I/O. Locks the binary-unit thresholds, the
one-decimal-place formatting, and the negative/TB edge cases the reference
formatter does not cover. (TODO #20)
"""
import pytest

from agent_windows.utils.formatting import format_size


class TestFormatSize:
    def test_zero_is_zero_bytes(self):
        assert format_size(0) == "0 B"

    def test_sub_kilobyte_stays_in_bytes(self):
        assert format_size(1) == "1 B"
        assert format_size(1023) == "1023 B"

    def test_exact_kilobyte_boundary(self):
        """1024 is the KB threshold (binary units, not 1000)."""
        assert format_size(1024) == "1.0 KB"

    def test_kilobyte_with_decimal(self):
        assert format_size(1536) == "1.5 KB"

    def test_megabyte_threshold(self):
        assert format_size(1024 ** 2) == "1.0 MB"
        assert format_size(1572864) == "1.5 MB"  # 1.5 MiB

    def test_gigabyte_threshold(self):
        assert format_size(1024 ** 3) == "1.0 GB"

    def test_terabyte_does_not_mislabel_as_gb(self):
        """Values >= 1 TiB must label as TB, not roll over to a huge GB number
        (the reference formatter lacks this case)."""
        assert format_size(1024 ** 4) == "1.0 TB"
        assert format_size(1024 ** 5) == "1024.0 TB"  # past the ladder

    def test_negative_returns_raw_bytes(self):
        """A negative size (shouldn't happen in practice) is reported as raw
        bytes rather than crashing or producing a nonsense unit."""
        assert format_size(-512) == "-512 B"

    @pytest.mark.parametrize("value,expected", [
        (512, "512 B"),
        (2048, "2.0 KB"),
        (5 * 1024 ** 2, "5.0 MB"),
        (10 * 1024 ** 3, "10.0 GB"),
    ])
    def test_round_numbers(self, value, expected):
        assert format_size(value) == expected
