"""Tests for core/registry.py — Windows Registry operations via PowerShell.

Pure-function tests: subprocess.run is patched so no real PowerShell or registry
access happens. The focus is the security boundary (PS single-quote escaping of
every interpolated string — the injection vector), type encoding, hive/path
validation, DoS bounds, and output shape.
"""

import subprocess
from unittest.mock import patch, MagicMock

import pytest

from agent_windows.core import registry
from agent_windows.core.registry import (
    _quote, _validate_path, _validate_name, _encode_value, _display,
    get_value, set_value, delete_entry, list_key,
    ALLOWED_HIVES, ALLOWED_TYPES, MAX_PATH_LEN, MAX_NAME_LEN,
)


def _ok_run(stdout="", stderr="", returncode=0):
    r = MagicMock()
    r.stdout = stdout
    r.stderr = stderr
    r.returncode = returncode
    return r


def _captured_command(mock_run):
    """Return the PowerShell command string from the FIRST patched subprocess.run
    call. delete_entry now makes a second list call (P1.3 recheck), so tests
    asserting the delete command read the first invocation."""
    return mock_run.call_args_list[0].args[0][-1]


# ── _quote (the injection guard) ────────────────────────────────────


class TestQuote:
    def test_plain_string(self):
        assert _quote("abc") == "'abc'"

    def test_single_quote_escaped(self):
        """A single quote is doubled — PowerShell's own escape inside a
        single-quoted literal — so it can't break out of the quotes."""
        assert _quote("a'b") == "'a''b'"

    def test_injection_payload_contained(self):
        hostile = "'; Get-Process; '"
        quoted = _quote(hostile)
        # The whole payload is inside one single-quoted literal; the internal
        # quotes are doubled, so PowerShell sees it as one inert string.
        assert quoted == "'''; Get-Process; '''"
        # Crucially, no unescaped single quote breaks the literal.
        # Count unescaped quotes: an even number means balanced literals.
        stripped = quoted.replace("''", "")
        assert stripped.count("'") % 2 == 0

    def test_empty_string(self):
        assert _quote("") == "''"


# ─_validate_path (hive whitelist) ──────────────────────────────────


class TestPathValidation:
    @pytest.mark.parametrize("hive", sorted(ALLOWED_HIVES))
    def test_allowed_hives_accepted(self, hive):
        for path in (f"{hive}:\\Software", f"{hive}\\Software", f"{hive}:Software"):
            _validate_path(path)  # no raise

    def test_lowercase_hive_accepted(self):
        _validate_path("hkcu:\\software")  # case-insensitive

    def test_bad_hive_rejected(self):
        with pytest.raises(ValueError, match="must start with one of"):
            _validate_path("HKXX:\\Software")

    def test_non_hive_prefix_rejected(self):
        with pytest.raises(ValueError, match="must start with one of"):
            _validate_path(r"C:\Users\evil")

    def test_empty_path_rejected(self):
        with pytest.raises(ValueError, match="non-empty string"):
            _validate_path("")

    def test_overlong_path_rejected(self):
        with pytest.raises(ValueError, match="path exceeds"):
            _validate_path("HKCU:\\" + "x" * (MAX_PATH_LEN + 1))

    def test_non_string_rejected(self):
        with pytest.raises(ValueError, match="non-empty string"):
            _validate_path(123)  # type: ignore[arg-type]


class TestNameValidation:
    def test_ok(self):
        _validate_name("MyValue")

    def test_empty_rejected(self):
        with pytest.raises(ValueError, match="non-empty string"):
            _validate_name("")

    def test_overlong_rejected(self):
        with pytest.raises(ValueError, match="name exceeds"):
            _validate_name("x" * (MAX_NAME_LEN + 1))


# ─_encode_value (typed values) ────────────────────────────────────


class TestEncodeValue:
    def test_string(self):
        assert _encode_value("hello", "String") == "'hello'"

    def test_expandstring(self):
        assert _encode_value("%PATH%", "ExpandString") == "'%PATH%'"

    def test_dword_int(self):
        assert _encode_value(42, "DWord") == "42"

    def test_dword_str_decimal(self):
        assert _encode_value("42", "DWord") == "42"

    def test_dword_str_hex(self):
        assert _encode_value("0xff", "DWord") == "255"

    def test_dword_rejects_non_int(self):
        with pytest.raises(ValueError, match="must be an integer"):
            _encode_value("abc", "DWord")

    def test_dword_rejects_bool(self):
        with pytest.raises(ValueError, match="not bool"):
            _encode_value(True, "DWord")

    def test_dword_range(self):
        with pytest.raises(ValueError, match="32 bits"):
            _encode_value(-1, "DWord")
        with pytest.raises(ValueError, match="32 bits"):
            _encode_value(1 << 32, "DWord")

    def test_qword_range(self):
        assert _encode_value(1 << 33, "QWord") == str(1 << 33)
        with pytest.raises(ValueError, match="64 bits"):
            _encode_value(1 << 64, "QWord")

    def test_multistring(self):
        assert _encode_value(["a", "b"], "MultiString") == "@('a','b')"

    def test_multistring_rejects_non_list(self):
        with pytest.raises(ValueError, match="list of strings"):
            _encode_value("a", "MultiString")

    def test_multistring_rejects_non_str_element(self):
        with pytest.raises(ValueError, match="list of strings"):
            _encode_value(["a", 1], "MultiString")  # type: ignore[list-item]

    def test_binary(self):
        assert _encode_value("deadbeef", "Binary") == "([byte[]](0xde,0xad,0xbe,0xef))"

    def test_binary_uppercase(self):
        assert _encode_value("DEADBEEF", "Binary") == "([byte[]](0xde,0xad,0xbe,0xef))"

    def test_binary_strips_0x_prefix(self):
        # G2: a ``0x`` prefix passes validation (int(hexstr, 16) accepts it) but
        # used to render as ``0x0x,de,ad`` (the prefix re-sliced as a byte). It
        # must render identically to the unprefixed form.
        assert (
            _encode_value("0xdeadbeef", "Binary")
            == "([byte[]](0xde,0xad,0xbe,0xef))"
        )
        # Capitalized prefix too.
        assert (
            _encode_value("0XDEADBEEF", "Binary")
            == "([byte[]](0xde,0xad,0xbe,0xef))"
        )

    def test_binary_rejects_odd_length(self):
        with pytest.raises(ValueError, match="even number"):
            _encode_value("abc", "Binary")

    def test_binary_rejects_non_hex(self):
        with pytest.raises(ValueError, match="valid hexadecimal"):
            _encode_value("xy", "Binary")

    def test_string_value_rejects_non_str(self):
        with pytest.raises(ValueError, match="must be a string"):
            _encode_value(123, "String")  # type: ignore[arg-type]


# ─_get/set/delete/list: command construction + injection ──────────


class TestGetValue:
    def test_command_uses_quoted_path_name(self):
        # Get-Item + GetValueKind + GetValue; the quoted path/name appear in
        # each interpolated slot. No more -ExpandProperty.
        with patch("subprocess.run", return_value=_ok_run(
                stdout='{"Kind":"String","Value":"x"}')) as m:
            get_value(r"HKCU:\Software\MyApp", "Count")
        cmd = _captured_command(m)
        assert "Get-Item -Path 'HKCU:\\Software\\MyApp'" in cmd
        assert "GetValueKind('Count')" in cmd
        assert "GetValue('Count')" in cmd
        assert "-ExpandProperty" not in cmd

    def test_returns_value_on_success(self):
        with patch("subprocess.run", return_value=_ok_run(
                stdout='{"Kind":"String","Value":"hello"}')):
            r = get_value(r"HKCU:\Soft", "Name")
        assert r["value"] == "hello"
        assert r["reg_type"] == "String"
        assert r["exit_code"] == 0
        assert "hello" in r["output"]
        assert "type: String" in r["output"]

    def test_returns_dword_as_int_with_type(self):
        """P3.5: a DWord is returned as a native int (not the stringed '42'),
        with reg_type=DWord — so it's distinguishable from a String '42'."""
        with patch("subprocess.run", return_value=_ok_run(
                stdout='{"Kind":"DWord","Value":42}')):
            r = get_value(r"HKCU:\Soft", "Count")
        assert r["value"] == 42
        assert isinstance(r["value"], int)
        assert r["reg_type"] == "DWord"
        assert "= 42" in r["output"]
        assert "type: DWord" in r["output"]

    def test_returns_multistring_as_list(self):
        with patch("subprocess.run", return_value=_ok_run(
                stdout='{"Kind":"MultiString","Value":["a","b"]}')):
            r = get_value(r"HKCU:\Soft", "Lines")
        assert r["value"] == ["a", "b"]
        assert r["reg_type"] == "MultiString"
        assert "a; b" in r["output"]

    def test_returns_binary_as_int_list_displayed_hex(self):
        """Binary sibling is the raw byte list; the output line renders hex so
        it round-trips with `set --type Binary`'s hex input."""
        with patch("subprocess.run", return_value=_ok_run(
                stdout='{"Kind":"Binary","Value":[222,173,190,239]}')):
            r = get_value(r"HKCU:\Soft", "Blob")
        assert r["value"] == [222, 173, 190, 239]
        assert r["reg_type"] == "Binary"
        assert "deadbeef" in r["output"]

    def test_returns_none_on_error_no_raise(self):
        """A missing value is routine, not a crash — surface stderr, don't raise."""
        with patch("subprocess.run", return_value=_ok_run(stderr="not found", returncode=1)):
            r = get_value(r"HKCU:\Soft", "Nope")
        assert r["value"] is None
        assert r["reg_type"] is None
        assert r["exit_code"] == 1
        assert "not found" in r["output"]

    def test_invalid_json_falls_back_to_error(self):
        """Unexpected (non-JSON) stdout must not crash — surface as error,
        value/reg_type None. (Defensive; shouldn't happen in practice.)"""
        with patch("subprocess.run", return_value=_ok_run(stdout="not json", returncode=0)):
            r = get_value(r"HKCU:\Soft", "Name")
        assert r["value"] is None
        assert r["reg_type"] is None
        assert "Error reading registry" in r["output"]

    def test_injection_in_name_contained(self):
        """A hostile name can't break out of the quoted literal — it's
        interpolated twice (GetValueKind + GetValue), both via _quote."""
        hostile = "'; Get-Process; '"
        with patch("subprocess.run", return_value=_ok_run(
                stdout='{"Kind":"String","Value":"x"}')) as m:
            get_value(r"HKCU:\Soft", hostile)
        cmd = _captured_command(m)
        # The escaped literal appears in BOTH positions (GetValueKind + GetValue).
        escaped = "'''; Get-Process; '''"
        assert cmd.count(escaped) == 2
        # No unescaped breakout: collapse every doubled quote pair ('' → ''),
        # then every remaining single quote must be a LITERAL delimiter (balanced,
        # even count). Hostile content can't introduce an unbalanced quote.
        collapsed = cmd.replace("''", "")
        assert collapsed.count("'") % 2 == 0


class TestDisplay:
    """_display renders native-typed values for the human-readable output line."""

    def test_none_is_empty(self):
        assert _display(None) == ""

    def test_int_bare(self):
        assert _display(42) == "42"

    def test_str_as_is(self):
        assert _display("hello") == "hello"

    def test_multistring_joined(self):
        assert _display(["a", "b"]) == "a; b"

    def test_binary_as_hex(self):
        # round-trips with set --type Binary's hex input
        assert _display([222, 173, 190, 239]) == "deadbeef"


class TestSetValue:
    def test_command_creates_key_and_sets(self):
        with patch("subprocess.run", return_value=_ok_run()) as m:
            set_value(r"HKCU:\Soft\MyApp", "Count", 42, "DWord")
        cmd = _captured_command(m)
        assert "Test-Path" in cmd
        assert "New-Item" in cmd
        assert "Set-ItemProperty" in cmd
        assert "-Type DWord" in cmd
        assert "'Count'" in cmd

    def test_string_value_quoted(self):
        with patch("subprocess.run", return_value=_ok_run()) as m:
            set_value(r"HKCU:\Soft", "Name", "hello", "String")
        cmd = _captured_command(m)
        assert "-Value 'hello'" in cmd

    def test_injection_in_value_contained(self):
        """The main security test: a hostile value is escaped, never executed."""
        hostile = "'; Remove-Item C:\\evil; '"
        with patch("subprocess.run", return_value=_ok_run()) as m:
            set_value(r"HKCU:\Soft", "Name", hostile, "String")
        cmd = _captured_command(m)
        # The value is fully inside one single-quoted literal with doubled
        # internal quotes — never an unescaped breakout.
        expected_value = "'''; Remove-Item C:\\evil; '''"
        assert f"-Value {expected_value}" in cmd

    def test_invalid_type_rejected(self):
        with pytest.raises(ValueError, match="invalid registry type"):
            set_value(r"HKCU:\Soft", "Name", "v", "Bogus")

    def test_bad_value_type_rejected_before_subprocess(self):
        """A non-int DWord value never reaches PowerShell."""
        with patch("subprocess.run") as m:
            with pytest.raises(ValueError, match="must be an integer"):
                set_value(r"HKCU:\Soft", "Name", "abc", "DWord")
        m.assert_not_called()


class TestDeleteEntry:
    def test_delete_value(self):
        with patch("subprocess.run", return_value=_ok_run()) as m:
            delete_entry(r"HKCU:\Soft", "Name")
        cmd = _captured_command(m)
        assert "Remove-ItemProperty" in cmd
        assert "'Name'" in cmd

    def test_delete_key_when_no_name(self):
        with patch("subprocess.run", return_value=_ok_run()) as m:
            delete_entry(r"HKCU:\Soft")
        cmd = _captured_command(m)
        assert "Remove-Item -Path" in cmd
        assert "-Recurse -Force" in cmd
        assert "Remove-ItemProperty" not in cmd

    def test_delete_default_value_when_empty_name(self):
        """BUG-B fix (core layer): name='' targets the unnamed (default) value,
        NOT the whole key. PowerShell's default value is named '(default)'."""
        with patch("subprocess.run", return_value=_ok_run()) as m:
            delete_entry(r"HKCU:\Soft", name="")
        cmd = _captured_command(m)
        assert "Remove-ItemProperty" in cmd
        assert "(default)" in cmd
        # Must NOT be the whole-key delete.
        assert "-Recurse -Force" not in cmd


class TestDeleteRecheck:
    """P1.3: delete_entry lists remaining siblings after a successful delete —
    same-key value names for a value delete, parent sub-key names for a
    whole-key delete — so the agent knows what's left without a second call."""

    def test_delete_value_lists_remaining_values(self):
        # call 1 = delete (rc 0); call 2 = list (stdout "A\nB")
        runs = [_ok_run(), _ok_run(stdout="A\nB")]
        with patch("subprocess.run", side_effect=runs):
            r = delete_entry(r"HKCU:\Soft", "Name")
        assert "Remaining values:" in r["output"]
        assert "A, B" in r["output"]
        assert r["remaining"] == ["A", "B"]

    def test_delete_value_list_cmd_has_no_ps_filter(self):
        # The list call must NOT carry a ``-notlike 'PS*'`` filter: ``.Property``
        # holds value names only (the synthetic PSPath/PSParentPath props live on
        # the RegistryKey object, not in .Property), so the filter is redundant
        # AND lossy — a real value named PSData/PSVersion would be dropped.
        # Assert on the PS command string: the canned-stdout tests above mock
        # out subprocess.run, so PowerShell-side filtering never runs there.
        runs = [_ok_run(), _ok_run(stdout="A")]
        with patch("subprocess.run", side_effect=runs) as m:
            delete_entry(r"HKCU:\Soft", "Name")
        list_cmd = m.call_args_list[1].args[0][-1]  # 2nd run → list command
        assert "Where-Object" not in list_cmd
        assert "-notlike" not in list_cmd
        assert ".Property" in list_cmd

    def test_delete_value_empty_remaining(self):
        runs = [_ok_run(), _ok_run(stdout="")]
        with patch("subprocess.run", side_effect=runs):
            r = delete_entry(r"HKCU:\Soft", "Name")
        assert "(none)" in r["output"]
        assert r["remaining"] == []

    def test_delete_key_lists_parent_subkeys(self):
        runs = [_ok_run(), _ok_run(stdout="Sub1\nSub2")]
        with patch("subprocess.run", side_effect=runs):
            r = delete_entry(r"HKCU:\Soft")  # name=None → whole key
        assert "Remaining sub-keys:" in r["output"]
        assert "Sub1, Sub2" in r["output"]
        assert r["remaining"] == ["Sub1", "Sub2"]

    def test_delete_default_value_lists_values(self):
        runs = [_ok_run(), _ok_run(stdout="Only")]
        with patch("subprocess.run", side_effect=runs):
            r = delete_entry(r"HKCU:\Soft", name="")
        assert "Remaining values:" in r["output"]

    def test_delete_failed_no_list(self):
        # delete rc!=0 → keep the error output, no recheck (single subprocess)
        with patch("subprocess.run",
                   return_value=_ok_run(stderr="not found", returncode=1)) as m:
            r = delete_entry(r"HKCU:\Soft", "Name")
        assert "Error deleting" in r["output"]
        assert "remaining" not in r
        assert m.call_count == 1

    def test_delete_value_list_failure_surfaces_not_silent_none(self):
        """list failure (e.g. access denied enumerating the key) must NOT read
        as '(none)' survivors — that masks a real error behind an empty list.
        Surface the list failure instead, with a list_failed sibling."""
        runs = [_ok_run(), _ok_run(stderr="Access is denied", returncode=1)]
        with patch("subprocess.run", side_effect=runs):
            r = delete_entry(r"HKCU:\Soft", "Name")
        assert "(none)" not in r["output"]
        assert "list failed" in r["output"].lower()
        assert r["list_failed"] is True
        assert r["remaining"] == []

    def test_delete_value_list_cmd_injects_escaped_path(self):
        """The list call reuses q_path (Get-Item) — a hostile path must stay
        escaped there too, not only in the delete command. Mirrors
        TestGetValue.test_injection_in_name_contained: assert the escaped
        literal appears and no unescaped quote breaks out."""
        hostile = r"HKCU:\Soft'; Remove-Item C:\evil; '"
        runs = [_ok_run(), _ok_run(stdout="A")]
        with patch("subprocess.run", side_effect=runs) as m:
            delete_entry(hostile, "Name")
        list_cmd = m.call_args_list[1].args[0][-1]  # 2nd run → list command
        # The escaped path literal is in the list command (single Get-Item).
        assert _quote(hostile) in list_cmd
        # No unescaped breakout: collapse every doubled quote pair, then every
        # remaining single quote must be a LITERAL delimiter (balanced, even).
        collapsed = list_cmd.replace("''", "")
        assert collapsed.count("'") % 2 == 0


class TestListKey:
    def test_command_lists_values_and_subkeys(self):
        with patch("subprocess.run", return_value=_ok_run(stdout="Values:\nx=1")) as m:
            r = list_key(r"HKCU:\Soft")
        cmd = _captured_command(m)
        assert "Get-ItemProperty" in cmd
        assert "Get-ChildItem" in cmd
        assert "Values:\nx=1" in r["output"]

    def test_bad_path_rejected(self):
        with pytest.raises(ValueError, match="must start with one of"):
            list_key(r"C:\bad")

    def test_bad_path_never_reaches_subprocess(self):
        """A bad hive is rejected before any PowerShell call (GAP-D fix)."""
        with patch("subprocess.run") as m:
            with pytest.raises(ValueError, match="must start with one of"):
                list_key(r"HKXX:\bad")
        m.assert_not_called()


# ─ error paths ──────────────────────────────────────────────────────


class TestErrorPaths:
    def test_timeout_raises_runtime_error(self):
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="ps", timeout=30)):
            with pytest.raises(RuntimeError, match="timed out"):
                get_value(r"HKCU:\Soft", "Name")

    def test_missing_powershell_raises_runtime_error(self):
        with patch("subprocess.run", side_effect=FileNotFoundError()):
            with pytest.raises(RuntimeError, match="PowerShell not found"):
                get_value(r"HKCU:\Soft", "Name")
