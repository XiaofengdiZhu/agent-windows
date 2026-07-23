"""Windows Registry operations via PowerShell cmdlets. (TODO #11)

get/set/delete/list with typed values. Paths use PowerShell drive format
(``HKCU:\\Software\\MyApp``). Implemented through ``Get-ItemProperty`` /
``Set-ItemProperty`` / ``Remove-Item[Property]`` invoked from Windows PowerShell.

Security model: every user-controlled string (path/name/value) is interpolated
into the PowerShell command, so each is escaped with ``'`` → ``''`` and wrapped
in a single-quoted literal — PowerShell's own escape inside a single-quoted
string, the same pattern ``core/process_manager.py`` uses. This is fully
general (accepts any content) and closes the injection surface: a payload like
``'; Remove-Item C:\\; '`` becomes the inert literal ``'''; Remove-Item C:\\; '''``.
No shell blacklist is applied — the agent already has unrestricted ``shell``,
so this tool grants no privilege escalation beyond it. DoS bounds cap the
path/name/value lengths.
"""

import logging
import subprocess
from typing import Any

logger = logging.getLogger(__name__)

# PowerShell drive aliases for the registry hives. The path's first component
# (before ':' or '\') must be one of these.
ALLOWED_HIVES = frozenset({"HKCU", "HKLM", "HKCR", "HKU", "HKCC", "HKPD"})

# Registry value types accepted by ``Set-ItemProperty -Type``.
ALLOWED_TYPES = frozenset(
    {"String", "ExpandString", "Binary", "DWord", "MultiString", "QWord"}
)

# DoS bounds: cap inputs so a pathological path/name/value can't bloat the PS
# argument line or hang the single STA worker. 255 is the registry's real
# value-name limit; 1 MiB is generous for a value (registry cells are smaller).
MAX_PATH_LEN = 2048
MAX_NAME_LEN = 255
MAX_VALUE_LEN = 1024 * 1024


def _quote(value: str) -> str:
    """Wrap a string in a PowerShell single-quoted literal, escaping ``'`` → ``''``.

    This is PowerShell's own escape inside a single-quoted string, so the
    result is always one literal token regardless of content — the same
    technique ``core/process_manager.py`` uses. A payload ``a'b`` becomes
    ``'a''b'`` (the literal string ``a'b``), never breaking out of the quotes.
    """
    return "'" + value.replace("'", "''") + "'"


def _validate_path(path: str) -> None:
    """Validate the registry hive root and length. Raise ValueError on a bad hive.

    Accepts ``HKCU:\\...``, ``HKCU\\...``, or ``HKCU:...``. The hive is the
    longest leading run of word chars; it must be in ALLOWED_HIVES.
    """
    if not isinstance(path, str) or not path:
        raise ValueError("path must be a non-empty string")
    if len(path) > MAX_PATH_LEN:
        raise ValueError(f"path exceeds {MAX_PATH_LEN} characters")
    # Hive = leading run of [A-Za-z0-9_], case-insensitive.
    rest = path.lstrip()
    i = 0
    while i < len(rest) and (rest[i].isalnum() or rest[i] == "_"):
        i += 1
    hive = rest[:i].upper()
    if hive not in ALLOWED_HIVES:
        raise ValueError(
            f"registry path must start with one of "
            f"{', '.join(sorted(ALLOWED_HIVES))} (got {hive or 'empty'!r})"
        )


def _validate_name(name: str) -> None:
    if not isinstance(name, str) or not name:
        raise ValueError("name must be a non-empty string")
    if len(name) > MAX_NAME_LEN:
        raise ValueError(f"name exceeds {MAX_NAME_LEN} characters")


def _encode_value(value: Any, reg_type: str) -> str:
    """Convert a Python value to its PowerShell representation for
    ``Set-ItemProperty -Value``, validating per-type constraints.

    - String/ExpandString: any string.
    - DWord/QWord: an int (or int-parseable), rendered bare (PS integer literal).
    - MultiString: a list[str], rendered as a PS array ``@('a','b')``.
    - Binary: a hex string (e.g. ``deadbeef``), rendered as ``([byte[]](0xde,0xad,...))``.
    """
    if reg_type in ("String", "ExpandString"):
        if not isinstance(value, str):
            raise ValueError(f"value for {reg_type} must be a string")
        if len(value) > MAX_VALUE_LEN:
            raise ValueError(f"value exceeds {MAX_VALUE_LEN} characters")
        return _quote(value)

    if reg_type in ("DWord", "QWord"):
        # Accept int, or a str that parses as int (the CLI passes strings).
        if isinstance(value, bool):
            raise ValueError(f"value for {reg_type} must be an integer, not bool")
        if isinstance(value, int):
            ivalue = value
        elif isinstance(value, str):
            try:
                ivalue = int(value, 0) if value.strip().lower().startswith("0x") else int(value)
            except ValueError:
                raise ValueError(f"value for {reg_type} must be an integer, got {value!r}")
        else:
            raise ValueError(f"value for {reg_type} must be an integer, got {type(value).__name__}")
        bits = 32 if reg_type == "DWord" else 64
        lo, hi = 0, (1 << bits) - 1
        if not (lo <= ivalue <= hi):
            raise ValueError(f"value for {reg_type} must fit in {bits} bits (0..{hi})")
        return str(ivalue)

    if reg_type == "MultiString":
        if not isinstance(value, list) or not all(isinstance(s, str) for s in value):
            raise ValueError("value for MultiString must be a list of strings")
        if sum(len(s) for s in value) > MAX_VALUE_LEN:
            raise ValueError(f"value exceeds {MAX_VALUE_LEN} characters")
        elements = ",".join(_quote(s) for s in value)
        return f"@({elements})"

    if reg_type == "Binary":
        if not isinstance(value, str):
            raise ValueError("value for Binary must be a hex string")
        hexstr = value.strip()
        # Strip an optional ``0x``/``0X`` prefix. int(hexstr, 16) accepts it, so
        # without stripping a value like "0xdeadbeef" passes validation but the
        # render loop below re-slices the prefix as a byte → "0x0x,de,ad,...".
        # DWord/QWord already normalize via int(value, 0); Binary is the str
        # path and must normalize the same way. (G2)
        if hexstr[:2].lower() == "0x":
            hexstr = hexstr[2:]
        if len(hexstr) % 2 != 0:
            raise ValueError("Binary value must have an even number of hex digits")
        try:
            bytes(hexstr, "ascii")  # ensure ascii
            int(hexstr, 16)  # validate hex
        except (ValueError, UnicodeEncodeError):
            raise ValueError("Binary value must be valid hexadecimal")
        if len(hexstr) // 2 > MAX_VALUE_LEN:
            raise ValueError(f"value exceeds {MAX_VALUE_LEN} bytes")
        # Render as a PS byte array: ([byte[]](0xde,0xad,...))
        octets = ",".join(f"0x{hexstr[i:i+2].lower()}" for i in range(0, len(hexstr), 2))
        return f"([byte[]]({octets}))"

    raise ValueError(f"unsupported registry type {reg_type!r}")


def _run(command: str, timeout: float = 30.0) -> dict[str, Any]:
    """Run a PowerShell command; return output/exit_code/stdout/stderr.

    A nonzero exit surfaces stderr in ``output`` WITHOUT raising — a get on a
    missing value is routine, not a crash (matching the reference's lenient
    style). Timeout raises RuntimeError → DAEMON_INTERNAL_ERROR.
    """
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace",
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"Registry command timed out after {timeout}s")
    except FileNotFoundError:
        raise RuntimeError("PowerShell not found")
    return {
        "exit_code": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }


def get_value(path: str, name: str) -> dict[str, Any]:
    """Read a registry value."""
    _validate_path(path)
    _validate_name(name)
    q_path, q_name = _quote(path), _quote(name)
    command = (
        f"Get-ItemProperty -Path {q_path} -Name {q_name} | "
        f"Select-Object -ExpandProperty {q_name}"
    )
    r = _run(command)
    value = r["stdout"].strip()
    out = (
        f'Registry value [{path}] "{name}" = {value}'
        if r["exit_code"] == 0
        else f'Error reading registry: {r["stderr"].strip() or r["stdout"].strip()}'
    )
    return {"output": out, "value": value if r["exit_code"] == 0 else None, **r}


def set_value(path: str, name: str, value: Any, reg_type: str = "String") -> dict[str, Any]:
    """Create or update a registry value, creating the key if absent."""
    if reg_type not in ALLOWED_TYPES:
        raise ValueError(
            f"invalid registry type {reg_type!r}; allowed: {', '.join(sorted(ALLOWED_TYPES))}"
        )
    _validate_path(path)
    _validate_name(name)
    ps_value = _encode_value(value, reg_type)
    q_path, q_name = _quote(path), _quote(name)
    command = (
        f"if (-not (Test-Path {q_path})) {{ New-Item -Path {q_path} -Force | Out-Null }}; "
        f"Set-ItemProperty -Path {q_path} -Name {q_name} -Value {ps_value} -Type {reg_type} -Force"
    )
    r = _run(command)
    out = (
        f'Registry value [{path}] "{name}" set (type: {reg_type}).'
        if r["exit_code"] == 0
        else f'Error writing registry: {r["stderr"].strip() or r["stdout"].strip()}'
    )
    return {"output": out, **r}


def delete_entry(path: str, name: str | None = None) -> dict[str, Any]:
    """Delete a registry value, the unnamed default value, or the whole key.

    - ``name is None``  → delete the whole key (recursive).
    - ``name == ""``    → delete the unnamed ``(default)`` value. PowerShell's
      ``Remove-ItemProperty -Name ''`` does NOT target the default value (which
      is named ``(default)`` internally), so use ``(Get-Item ...).'(default)'``
      via ``Remove-ItemProperty`` on the property named ``(default)``.
    - ``name`` non-empty → delete that named value.

    On a successful delete the result carries a ``remaining`` sibling — the
    surviving value names (value delete) or sub-key names (whole-key delete) of
    the same key/parent, so the agent knows what's left without a second call.
    If that recheck list itself fails (e.g. access denied), ``list_failed`` is
    set instead of silently reporting an empty list.
    """
    _validate_path(path)
    q_path = _quote(path)
    if name is None:
        command = f"Remove-Item -Path {q_path} -Recurse -Force"
        what = "key"
    elif name == "":
        # The unnamed default value. ``Remove-ItemProperty -Name '(default)'``
        # is the reliable way (the property is literally named "(default)").
        command = f"Remove-ItemProperty -Path {q_path} -Name '(default)' -Force"
        what = 'value "(default)"'
    else:
        _validate_name(name)
        q_name = _quote(name)
        command = f"Remove-ItemProperty -Path {q_path} -Name {q_name} -Force"
        what = f'value "{name}"'
    r = _run(command)
    if r["exit_code"] != 0:
        return {"output": f'Error deleting registry {what}: '
                          f'{r["stderr"].strip() or r["stdout"].strip()}', **r}
    # P1.3: list remaining siblings so the agent knows what's left without a
    # second call. A value delete lists the same key's surviving value names;
    # a whole-key delete lists the parent's surviving sub-key names.
    if name is None:
        list_cmd = (f"(Get-ChildItem -Path (Split-Path {q_path} -Parent) "
                    f"-ErrorAction SilentlyContinue | "
                    f"Select-Object -ExpandProperty PSChildName) -join \"`n\"")
        list_what = "sub-keys"
    else:
        # .Property holds the key's value names only — the synthetic
        # PSPath/PSParentPath props live on the RegistryKey object, not in this
        # array — so no PS* filter is needed (and one would silently drop real
        # PS-prefixed values like PSData/PSVersion). Contrast list_key, which
        # runs on a Get-ItemProperty PSCustomObject where the synthetic props
        # ARE mixed in and DO need excluding.
        list_cmd = (f"((Get-Item {q_path} -ErrorAction SilentlyContinue).Property) "
                    f"-join \"`n\"")
        list_what = "values"
    lr = _run(list_cmd)
    if lr["exit_code"] != 0:
        # list failed (e.g. access denied enumerating the key) — must NOT read
        # as an empty "(none)" survivor list, which masks the error behind a
        # benign-looking empty result. Surface the failure instead.
        list_err = (lr["stderr"] or lr["stdout"]).strip() or f"exit code {lr['exit_code']}"
        return {"output": f"Registry {what} [{path}] deleted.\n"
                          f"Remaining {list_what}: (list failed: {list_err})",
                "remaining": [], "list_failed": True, **r}
    remaining = [s for s in lr["stdout"].splitlines() if s.strip()]
    rem_line = ", ".join(remaining) if remaining else "(none)"
    return {"output": f"Registry {what} [{path}] deleted.\n"
                      f"Remaining {list_what}: {rem_line}",
            "remaining": remaining, **r}


def list_key(path: str) -> dict[str, Any]:
    """List values and sub-keys under a path."""
    _validate_path(path)
    q_path = _quote(path)
    # Values: Get-ItemProperty minus the PS* synthetic props; sub-keys: child names.
    command = (
        f"$values = (Get-ItemProperty -Path {q_path} -ErrorAction Stop | "
        f"Select-Object * -ExcludeProperty PS* | Format-List | Out-String).Trim(); "
        f"$subkeys = (Get-ChildItem -Path {q_path} -ErrorAction SilentlyContinue | "
        f"Select-Object -ExpandProperty PSChildName) -join \"`n\"; "
        f"if ($values) {{ Write-Output \"Values:`n$values\" }}; "
        f"if ($subkeys) {{ Write-Output \"`nSub-Keys:`n$subkeys\" }}; "
        f"if (-not $values -and -not $subkeys) {{ Write-Output 'No values or sub-keys found.' }}"
    )
    r = _run(command)
    body = r["stdout"].strip()
    out = (
        f"Registry key [{path}]:\n{body}"
        if r["exit_code"] == 0
        else f'Error listing registry: {r["stderr"].strip() or r["stdout"].strip()}'
    )
    return {"output": out, **r}
