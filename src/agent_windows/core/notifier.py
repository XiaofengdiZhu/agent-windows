"""Windows toast notifications via PowerShell WinRT. (TODO #10)

Sends a toast (title + message) through the ``ToastNotificationManager`` WinRT
API, invoked from Windows PowerShell 5.1 (the WinRT toast types are NOT
available in PowerShell 7+/pwsh, so ``powershell`` is used explicitly, matching
the reference implementation).

Security model (mirrors ``core/clipboard.py``'s SEC-05 fix): the toast XML is
passed to PowerShell on **stdin** via a CONSTANT command that reads ``$input``
— the XML is never interpolated into the command string. This closes the
``$(...)`` / ``$var`` RCE vector an expanding here-string (``@"..."@``) would
open, because PowerShell never parses the user's title/message as code. The
XML itself is entity-escaped (so ``<``/``&`` are inert text nodes), and
``app_id`` — the only value interpolated into the command, via a PS
single-quoted literal — is rejected if it contains a single quote.
"""

import logging
import subprocess
from typing import Any

logger = logging.getLogger(__name__)

# Default AppUserModelID. PowerShell's own AUMID (under the System alias
# {1AC14E77-...}\WindowsPowerShell) is always present on stock Windows and
# displays a toast attributed to PowerShell without requiring a Start Menu
# shortcut registration — the friction-free default the caller can override
# with --app-id. A custom AUMID only displays reliably if a matching shortcut
# is registered, so we don't pretend a bare string "just works".
DEFAULT_APP_ID = (
    "{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe"
)

# DoS bound: a pathological title/message would bloat the toast XML and the
# stdin pipe. Cap each at a generous limit (toasts render only the first ~2-3
# lines anyway) so a runaway caller can't hang the single STA worker. Matches
# the project's DoS-conscious style (MAX_TYPE_TEXT_LEN, MAX_BATCH_SIZE).
MAX_TITLE_LEN = 256
MAX_MESSAGE_LEN = 1024
# app_id is interpolated into the command line; bound it so a multi-KiB value
# can't blow past the Windows CreateProcess argv limit (~32 KiB) and surface as
# a confusing FileNotFoundError. AUMIDs are short GUIDs/paths.
MAX_APP_ID_LEN = 1024


def _xml_escape(text: str) -> str:
    """Escape the 5 XML special characters and strip XML-1.0-illegal control chars.

    XML 1.0 forbids most C0 control codes (anything except ``\\t \\n \\r``);
    ``$xml.LoadXml`` throws on them inside PowerShell, surfacing as a cryptic
    ``sent=False`` instead of a clean INVALID_PARAMS. Strip them at the boundary
    so any string round-trips into a valid text node. The five entity chars are
    escaped so a payload like ``</text><script>`` renders as inert literal text.
    """
    # Drop C0 control chars except \t (0x09), \n (0x0a), \r (0x0d).
    cleaned = "".join(
        c for c in text if c >= " " or c in ("\t", "\n", "\r")
    )
    return (
        cleaned.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def _build_toast_xml(title: str, message: str) -> str:
    """Build the ToastGeneric XML template with escaped title/message."""
    return (
        "<toast>"
        "<visual>"
        '<binding template="ToastGeneric">'
        f"<text>{_xml_escape(title)}</text>"
        f"<text>{_xml_escape(message)}</text>"
        "</binding>"
        "</visual>"
        "</toast>"
    )


def _build_ps_command(app_id: str) -> str:
    """Build the CONSTANT PowerShell command (independent of title/message).

    The toast XML is piped in on stdin and read via ``$input``; it is NEVER
    interpolated into the command string, so ``$(...)``/``$var`` in the user's
    title/message can't execute as PowerShell (the SEC-05 lesson from
    ``core/clipboard.py``). Only ``app_id`` is interpolated, into a
    single-quoted literal (validated to contain no single quote by the caller).
    The WinRT types are loaded explicitly (PS 5.1 ships the WinMD projections;
    pwsh 7+ does not, hence ``powershell`` not ``pwsh``).
    """
    return (
        "[Windows.UI.Notifications.ToastNotificationManager, "
        "Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null; "
        "[Windows.Data.Xml.Dom.XmlDocument, "
        "Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null; "
        f"$appId = '{app_id}'; "
        "$xml = New-Object Windows.Data.Xml.Dom.XmlDocument; "
        "$xml.LoadXml(($input -join \"`n\")); "
        "$notifier = "
        "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($appId); "
        "$toast = New-Object Windows.UI.Notifications.ToastNotification $xml; "
        "$notifier.Show($toast)"
    )


def send_notification(
    title: str,
    message: str,
    app_id: str = DEFAULT_APP_ID,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """Send a Windows toast notification (title + message). (TODO #10)

    Args:
        title: Toast heading.
        message: Toast body text.
        app_id: AppUserModelID the toast is attributed to. Defaults to
            PowerShell's AUMID (always present, no registration needed); a
            custom AUMID needs a registered Start Menu shortcut to display.
        timeout: PowerShell execution timeout in seconds (30s; first-call
            WinRT type loading can be slow).

    Returns:
        Dict with ``output``, ``exit_code``, ``stdout``, ``stderr``,
        ``app_id``, ``sent`` (``sent`` is True only when PowerShell exited 0).

    Raises:
        ValueError: If title/message exceed their length caps, or ``app_id``
            is too long or contains a single quote (PS injection guard) —
            maps to JSON-RPC INVALID_PARAMS via the router.
        TimeoutError: On PowerShell timeout — maps to WAIT_TIMEOUT (-1003).
        RuntimeError: If PowerShell is missing.
    """
    if not isinstance(title, str) or not isinstance(message, str):
        raise ValueError("title and message must be strings")
    if len(title) > MAX_TITLE_LEN:
        raise ValueError(
            f"title exceeds {MAX_TITLE_LEN} characters (got {len(title)})"
        )
    if len(message) > MAX_MESSAGE_LEN:
        raise ValueError(
            f"message exceeds {MAX_MESSAGE_LEN} characters (got {len(message)})"
        )
    if len(app_id) > MAX_APP_ID_LEN:
        raise ValueError(
            f"app_id exceeds {MAX_APP_ID_LEN} characters (got {len(app_id)})"
        )
    # app_id is the only string placed in a PS single-quoted literal. A single
    # quote is the sole character that can break out of that context, so reject
    # it outright rather than trying to escape it (PowerShell's escape is ``''``,
    # but rejecting is simpler and the default covers the common case).
    if "'" in app_id:
        raise ValueError("app_id must not contain a single quote")

    # The XML rides on stdin; the command is a constant (no user content).
    command = _build_ps_command(app_id)
    toast_xml = _build_toast_xml(title, message)
    logger.debug("Sending toast: %s / %s", title, message)

    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
            input=toast_xml,
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
        )
    except subprocess.TimeoutExpired:
        # Raise TimeoutError (not RuntimeError) so the router maps it to
        # WAIT_TIMEOUT (-1003) instead of the generic DAEMON_INTERNAL_ERROR.
        raise TimeoutError(f"Toast notification timed out after {timeout}s")
    except FileNotFoundError:
        raise RuntimeError("PowerShell not found")

    sent = result.returncode == 0
    output_parts = [
        f"## Notify\nTitle: {title}\nMessage: {message}",
        f"App ID: {app_id}",
    ]
    if not sent:
        # PowerShell surfaced an error (often a missing/invalid AUMID). Include
        # stderr so the caller can diagnose, but don't raise — the toast may
        # still have shown, matching the reference's lenient handling.
        output_parts.append(f"[stderr]\n{result.stderr.strip()}")
    output_parts.append(f"Exit code: {result.returncode}")

    return {
        "output": "\n".join(output_parts),
        "exit_code": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "app_id": app_id,
        "sent": sent,
    }
