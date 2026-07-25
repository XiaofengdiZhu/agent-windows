<!-- Reference doc — loaded on demand via the table in SKILL.md, not discovered as a standalone skill. -->

# Troubleshooting

## Daemon Issues

### Daemon won't start

**Symptom**: `daemon start` fails or first CLI call hangs.

**Recovery**:
1. Check if a stale daemon is running: `agent-windows daemon status`
2. Kill it: `agent-windows daemon stop`
3. If that fails, kill the process manually: `agent-windows process kill agent-windows`
4. Retry: `agent-windows daemon start`

### Stale PID file

**Symptom**: `daemon status` shows daemon running but commands fail with connection error.

**Recovery**: The PID file at `~/.agent-windows/daemon.pid` may reference a dead process. Delete it and retry:

```bash
agent-windows daemon stop
# If that fails, delete the state file manually
agent-windows daemon start
```

### Version mismatch

**Symptom**: `daemon status` warns about version mismatch.

**Recovery**: The daemon should auto-restart. If not:

```bash
agent-windows daemon stop
agent-windows daemon start
```

### Daemon keeps restarting (uvx version drift)

**Symptom**: you run commands via `uvx agent-windows ...` and the daemon restarts itself on (seemingly) every few invocations, dropping UIA state and forcing re-`observe`.

**Cause**: `uvx` runs from an isolated, cached environment. When that cache gets refreshed (new release, cache expiry, `uvx -U`), the CLI version no longer matches the running daemon, and the version check restarts it.

**Recovery**: switch to a persistent install so CLI and daemon versions are pinned together:

```bash
uv tool install agent-windows
agent-windows daemon stop       # let the new install's daemon take over
agent-windows observe
```

### Connection refused

**Symptom**: "Failed to connect to daemon" error.

**Recovery**:
1. `agent-windows daemon start` to ensure daemon is running
2. If still failing, use `--no-daemon` to bypass: `agent-windows snapshot --no-daemon`
3. Check the named pipe exists: `\\.\pipe\agent-windows`

## "Element @eN not found"

**Error code**: `-1001` (ELEMENT_NOT_FOUND) or `-32602` (INVALID_PARAMS)

**Causes**:
1. **Stale refs**: The UI changed since the last snapshot. `@e3` now refers to a different element or is out of range.
2. **Element scrolled off-screen**: The element exists in the tree but is outside the visible area.
3. **Element in a different window**: The snapshot was taken on one window but you're now in another.

**Recovery**:
1. Re-observe: `agent-windows observe` or `agent-windows snapshot`
2. Find the target in the fresh Interactive Elements table
3. Use the new `@eN` ref and coordinates

## Click Landed in the Wrong Spot

**Causes**:
1. **Wrong window has focus**: The click went to the currently active window, not the intended one.
2. **DPI scaling**: Rare — DPI is handled automatically.
3. **Stale coordinates**: The element moved between snapshot and click.

**Recovery**:
1. Always `app switch "TargetWindow"` before interacting
2. Re-observe after any action that might shift the UI
3. Verify with `observe` after clicking

## Click / Type Silently Does Nothing on an Elevated Target (UIPI)

**Symptom**: `click`, `dblclick`, or `type` against an elevated/UAC-admin target (a Run-as-administrator window, Task Manager launched as admin, an admin consent dialog) silently does nothing, or returns `PERMISSION_DENIED (-1004)` with a message that begins `UIPI:`.

**Cause**: the daemon's integrity level (typically *medium*) is below the target's (typically *high*). Windows UIPI would silently drop `SendInput`-injected mouse/keyboard events at that window — the handler still reports success, leaving the agent in a retry loop. To prevent that, `daemon/handlers/input.py` runs a preflight on every foreground input path: `_require_uipi_clear_at(x, y)` for `click` / `dblclick` / `type --at` / `type --at-selector`, and `_require_uipi_clear_foreground()` for focused-field `type` (both wrappers live in `daemon/handlers/input.py`; they call the integrity-level checks in `core/uipi.py`). On an IL mismatch it raises a `PermissionError` whose message names both ILs and the recommended recovery; `route_request` maps that to `-1004` (`PERMISSION_DENIED`, router.py:225-228).

**Recovery**:
1. Retry with `--background` — the UIA `Invoke` / `Toggle` / `Select` patterns reach the target through the accessibility channel and bypass UIPI, so the action lands without stealing focus: `agent-windows click -s @e3 --background`
2. Or relaunch the daemon elevated so its own IL meets or exceeds the target's. Start `agent-windows` from an elevated shell; the spawned daemon inherits that IL — run `agent-windows daemon stop` first, then `agent-windows observe` from the elevated shell.

## Window Frozen After a Background Click

**Symptom**: After a `click --background`, the target window appears frozen —
the title-bar minimize/maximize/close buttons still work, but every button and
control in the window body is dead.

**Cause**: `--background` works by wrapping the UIA Invoke/Toggle/Select
pattern in `EnableWindow(host, FALSE) … pattern() … EnableWindow(host, TRUE)`.
The kernel accessibility channel carries the pattern while the host top-level
window *cannot* become foreground — no focus steal, no flash. A `finally` block
restores the window on a Python exception, but **not** if the daemon is killed
mid-bypass (e.g. `Stop-Process`, host crash) or the UIA `Invoke` call itself
hangs. The window is then stranded in the disabled state — only title-bar
chrome, which Win32 keeps on a separate code path, stays responsive.

**Recovery**: `window enable` re-enables the root window plus any disabled
descendants (UWP/WinUI content is hosted in the frame's child windows). It never
disables anything:

```bash
agent-windows window enable          # the frozen foreground window
agent-windows window enable "Calc"   # by title substring / HWND / process
```

The output reports how many windows were re-enabled (`enabled_count` /
`enabled` siblings). `0` means the window was already enabled — the freeze has
another cause (see "UIA Tree Is Empty or Incomplete"). From an MCP client the
tool is `window_enable`.

**Prevention**: don't kill the daemon while a `--background` click is in flight.
If a background click hangs, the per-submit `STA_SUBMIT_TIMEOUT_S` (300 s,
advisory) disconnects the request but cannot cancel the in-flight UIA call —
recover the stranded window with `window enable` afterward.

## Type Doesn't Work

**Causes**:
1. **IME interference**: `agent-windows type` uses Unicode direct input, which bypasses IME. If IME was active, the typed text may not appear correctly.
2. **Wrong field focused**: The text went to whatever control currently has keyboard focus, which may not be the intended target.
3. **Read-only field**: The target control doesn't accept text input.

**Recovery**:
1. Click the target field first: `agent-windows type "text" --at 300,200`
2. Use `--clear` to ensure the field is empty before typing: `agent-windows type "text" --clear`
3. Verify the field is focused by checking `[focused]` in the Interactive Elements table

## Vision Model Errors

**Error code**: `-1005` (VISION_API_ERROR)

**Causes**:
1. Invalid or missing API key
2. Model not found or not accessible
3. API rate limit or timeout
4. Network connectivity

**Recovery**:
1. Check API key: `agent-windows config doctor` (or `config show` and look for the `vlm.api_key` line)
2. Set the key. The cross-shell path is `agent-windows config set vlm.api_key "sk-..."` (stored encrypted). For an env var instead: `set AGENT_WINDOWS_VLM_API_KEY=sk-...` (cmd.exe) or `$env:AGENT_WINDOWS_VLM_API_KEY="sk-..."` (PowerShell).
3. Test with a simple call: `agent-windows ground "desktop"`
4. If timeout, increase: `agent-windows config set vlm.timeout 60`

## Wait Timeout

**Error code**: `-1003` (WAIT_TIMEOUT)

**Causes**:
1. The element/text genuinely never appeared within the timeout window.
2. The element exists but has a different name than expected.
3. The element is in a different window or tab.

**Recovery**:
1. Take a screenshot to see the current state: `agent-windows observe`
2. Check if the element exists in the snapshot with a different name
3. Increase the timeout: `agent-windows wait @e5 --timeout 30`
4. For text mode, try a substring: `agent-windows wait --text "完成" --timeout 30`

## UIA Tree Is Empty or Incomplete

**Causes**:
1. **App uses custom rendering**: Some apps (games, DirectX overlays, Firefox with IA2) don't fully expose UIA elements.
2. **`max_depth` too low**: Complex UIs may need deeper traversal.
3. **App not responding**: The target window is frozen or busy.

**Recovery**:
1. Increase depth: `agent-windows config set tree.max_depth 50`
2. Include offscreen elements: `agent-windows config set tree.include_offscreen true`
3. Use vision tools (`ground`, `detect`, `ask-vision`) for elements not visible to UIA

## UWP / Store App Snapshots as ApplicationFrameWindow

**Symptom**: snapshotting a UWP / Store app (Calculator, Photos, Settings) lists the host as `ApplicationFrameWindow` carrying an unexpected process name (`ApplicationFrameHost.exe`), or shows an empty frame body.

**Cause / reality**: UWP apps host their UI inside an `ApplicationFrameWindow` frame owned by `ApplicationFrameHost.exe`. The daemon resolves the *hosted* app's PID via `_resolve_uwp_app_pid` — an `EnumChildWindows` walk over the frame's children in `core/tree.py`, picking the first child whose pid differs from the frame host's — and normalizes the reported `process_name` to the app's real image (e.g. `Calculator.exe`). When the frame exists but the hosted app hasn't yet rendered into it (freshly launched, still initializing, or suspending/resuming), the resolver returns `None` and the frame host name is kept as the degrade value; the body may also appear empty until the app finishes loading.

**Recovery**:
1. Wait a moment and re-snapshot: `agent-windows snapshot --active`
2. Scope to a single window to skip unrelated frames: `agent-windows snapshot --window "Calculator"` (title substring, HWND, or process name — same resolution as `app switch`)
3. If the frame persists with an empty body, re-launch the app: `agent-windows app launch Calculator`

## Screenshot Looks Wrong

**Causes**:
1. **Multi-monitor capture**: `--monitor -1` captures all monitors as one large image.
2. **Scaling artifacts**: Rare with DPI V2 awareness enabled.

**Recovery**:
1. Use `--monitor 0` for primary display only
2. Check `max_width` / `max_height` config if images seem resized

## Error Codes

| Code | Name | Meaning |
|------|------|---------|
| `-32700` | `PARSE_ERROR` | Invalid JSON in request |
| `-32600` | `INVALID_REQUEST` | Malformed JSON-RPC request |
| `-32601` | `METHOD_NOT_FOUND` | Unknown command name |
| `-32602` | `INVALID_PARAMS` | Missing or invalid parameters (also: invalid @eN selector) |
| `-32603` | `INTERNAL_ERROR` | Unexpected server error |
| `-1001` | `ELEMENT_NOT_FOUND` | UIA element lookup failed |
| `-1002` | `COORDINATE_OUT_OF_RANGE` | Click/scroll coordinates outside screen bounds |
| `-1003` | `WAIT_TIMEOUT` | `wait`/`wait-for` exceeded timeout, or a `shell` command exceeded `shell.timeout` |
| `-1004` | `PERMISSION_DENIED` | UAC or access denied |
| `-1005` | `VISION_API_ERROR` | Vision model API call failed |
| `-1007` | `BACKGROUND_UNAVAILABLE` | A `--background` click could not land (no UIA element at the point, no Invoke/Toggle/Select pattern — often an occluded or UI-virtualized element, host window has no HWND, or the pattern COM call failed); retry foreground |
| `-1999` | `DAEMON_INTERNAL_ERROR` | Unhandled exception in daemon |

## Debug Mode

### Verbose Logging

```bash
agent-windows snapshot --debug
agent-windows click 500 300 --debug
```

The `--debug` flag enables verbose logging to stderr, showing internal operations, timing, and error details.

### JSON Output

```bash
agent-windows snapshot --format json
```

Returns structured JSON output instead of human-readable text. Useful for parsing error details and exact return values.

### Combined Debug

```bash
agent-windows click 500 300 --debug --format json --no-daemon
```

- `--debug`: Verbose logging
- `--format json`: Structured output
- `--no-daemon`: Run in-process, shows full stack traces

### Log Files

Daemon log files are stored in `~/.agent-windows/logs/`. Check these for daemon-side errors that don't surface in CLI output.
