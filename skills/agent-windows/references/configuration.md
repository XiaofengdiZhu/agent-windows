<!-- Reference doc — loaded on demand via the table in SKILL.md, not discovered as a standalone skill. -->

# Configuration Reference

## Config File

Path: `~/.agent-windows/config.toml`

### Management Commands

```bash
agent-windows config init              # Create default config file
agent-windows config show              # Print full config with file path
agent-windows config set key value     # Set a config value (dotted key notation)
agent-windows config doctor            # Check whether the vision model is configured
```

> The launcher forwards `config` to the bundled Python CLI transparently — no
> need to call `agent-windows-python` directly for config.

### `--config` override

Pass `--config <path>` to point at an alternate config file for one invocation
(useful for testing, CI, or multiple profiles without touching your real config):

```bash
agent-windows --config /tmp/profile.toml config show
agent-windows --config ~/profiles/ci.toml mcp serve
```

**Precedence:** `--config <path>` (if given) > `~/.agent-windows/config.toml`.

**Propagation:** `--config` applies to the current process *and* is forwarded
to the daemon the launcher spawns — so the daemon reads the same file. Without
this, the daemon would silently use the default while your CLI uses the override.

**Scope — config file only.** `--config` redirects config reads and writes
(`load_config`, `config set`, `mcp auth`). It does **not** move state files —
`daemon.pid`, `daemon.lock`, `daemon.version`, `logs/`, `screenshots/`, and
`prompts/` stay in `~/.agent-windows/`. Different profiles therefore share one
daemon instance and one log stream.

**Writes go to the override file.** When `--config` is set, `config set` and
`mcp auth` write to the override file (you pointed at it; writes follow). This
lets you fully manage an isolated profile.

**Missing file = defaults.** If the `--config` path does not exist, it's treated
as an empty config → merged defaults only (handy for testing against defaults
without creating a file). A file that exists but is unreadable or has invalid
TOML raises the usual error.

### TLS path resolution

Relative `[mcp] ssl_certfile` / `ssl_keyfile` paths are resolved against the
**config file's directory** (`get_config_path().parent`), not the process
working directory. This makes a config file self-contained: `mcp auth
--with-tls` writes absolute paths, but a hand-edited relative path like
`certs/server.pem` resolves next to the config regardless of where you launch
`mcp serve` from. Absolute paths and `~`-prefixed paths are used as-is.

If a configured TLS file is missing, `mcp serve` fails fast before uvicorn
starts, reporting the resolved absolute path and the config file location —
rather than surfacing as an opaque OpenSSL traceback. stdio transport ignores
TLS (local IPC), so the check is skipped there.

### Debug mode

Global debug state lives in the `AGENT_WINDOWS_DEBUG` environment variable. The
CLI/MCP/daemon entry points call `enable_debug()` when given `--debug`, which
sets that env var. Any module can read it with `is_debug()` (from
`agent_windows.utils.debug`) — no `--debug` boolean needs to be threaded
through function parameters. The spawned daemon subprocess inherits the env var
automatically, so debug mode crosses the process boundary without explicit
forwarding. `configure_logging()` raises the root logger plus the verbose
third-party loggers (`fastmcp`, `uvicorn.*`) to `DEBUG` when `is_debug()`; the
daemon uses its own `_setup_logging` (file + stderr handlers) but reads
`is_debug()` for the level decision.

Under debug mode, `snapshot` and `screenshot` emit **per-stage timing** lines to
the daemon log (`~/.agent-windows/logs/daemon.log`) for diagnosing slow captures —
never into the tool response, so clients are unaffected:

```
snapshot profile: enumerate_ms=2.3 traverse_ms=180.4 windows=12 elements=47 max_depth=15
screenshot profile: capture_ms=15.2 resize_ms=3.1 backend=dxcam annotated=False 1920x1080
```

`enumerate_ms` is the Win32 window enumeration; `traverse_ms` is the UIA COM
walk plus its setup (COM client init + cache build — the dominant cost, bounded
by `tree.max_depth`/`timeout`/`max_elements`); `max_depth` is the configured
depth cap (not the depth actually reached); `capture_ms` is the screen grab
(backend-dependent); `resize_ms` is the downscale. Compare stages across calls
to locate the bottleneck.

> **Annotated screenshots**: when `use_annotation` is on, `capture_ms` also
> includes the UIA tree snapshot (`get_tree_snapshot`) and the annotation
> drawing — that path additionally emits its own `snapshot profile:` line, so
> the same tree walk is timed twice (once inside `capture_ms`, once standalone).
> Read the standalone `snapshot profile:` line for the true tree cost on an
> annotated capture.

### Example

```bash
agent-windows config set vlm.model gpt-4o
agent-windows config set tree.max_depth 50
agent-windows config set wait.poll_interval 0.3
```

## Configuration Sections

### [vlm]

Vision model settings for `ground`, `detect`, and `ask-vision`.

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `enabled` | bool | `true` | Enable/disable vision features |
| `engine` | str | `"openai"` | Vision engine: `openai`, `anthropic`, or `gemini`. The aliases `doubao` and `gemini_compatible` route to the OpenAI-compatible path, and any unrecognized string also falls through to OpenAI-compatible |
| `model` | str | `"gpt-4o-mini"` | Default model for vision calls |
| `api_base` | str | `"https://api.openai.com/v1"` | API endpoint URL |
| `api_key` | str | `""` | API key. **Encrypted at rest** with Windows DPAPI (current-user scope). Set it via `config set vlm.api_key sk-...` — it is encrypted transparently on save and decrypted transparently on load. (Or use the `AGENT_WINDOWS_VLM_API_KEY` env var, which is never written to disk.) |
| `timeout` | int | `30` | Request timeout in seconds |
| `width` | int | `1000` | Screenshot resize width for model input |
| `height` | int | `1000` | Screenshot resize height for model input |
| `crop_size` | int | `400` | Crop width for fine-pass in `ground` |
| `crop_height` | int | `300` | Crop height for fine-pass in `ground` |
| `max_drift` | int | `40` | Max pixel drift for accepting fine-pass result |
| `image_format` | str | `"JPEG"` | Encoding format for images sent to the vision API: `JPEG` or `PNG` |
| `jpeg_quality` | int | `85` | JPEG quality (1-95) for vision API images. Ignored when `image_format = PNG` |
| `single_pass` | bool | `false` | Skip the fine-pass API call in `ground`: one network round-trip instead of two (faster, lower cost) at the cost of click precision — coarse coordinate only, no sub-element refinement. Override per call with `ground ... --single-pass`. |

> **Advanced — prompt overrides.** The built-in coarse/fine/detect VLM prompts can be overridden by a `[vlm.prompts]` table with `coarse`, `fine`, and `detect` string keys (each formatted with `{description}`; `detect` also gets `{max_targets}`). NOT in the settable schema — hand-edit `config.toml` only.

### [screenshot]

Screenshot capture and storage settings.

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `backend` | str | `"auto"` | Capture backend: `auto`, `dxcam`, `mss`, or `pillow` |
| `scale` | float | `1.0` | Screenshot scale factor |
| `max_width` | int | `1920` | Maximum screenshot width |
| `max_height` | int | `1080` | Maximum screenshot height |
| `format` | str | `"png"` | Image format: `png` or `jpeg` |
| `jpeg_quality` | int | `85` | JPEG quality (1-100, ignored for PNG) |
| `resampling` | str | `"BILINEAR"` | Downscale filter for saved screenshots: `BILINEAR`, `LANCZOS`, or `NEAREST` |
| `max_screenshot_age_days` | int | `7` | Auto-delete screenshots older than this |
| `max_screenshot_dir_mb` | int | `500` | Auto-cleanup when screenshot dir exceeds this size |
| `cleanup_interval_minutes` | int | `10` | How often to run auto-cleanup check |
| `dir` | str | `""` | Override the screenshot save directory. Empty (default) = `~/.agent-windows/screenshots/`. Unlike the keys above, this is NOT in the default TOML — it's an optional settable key read with a fallback. Set via `config set screenshot.dir "D:\\shots"`. |

### [tree]

UIA accessibility tree traversal settings.

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `include_invisible` | bool | `false` | Include invisible UIA elements |
| `include_offscreen` | bool | `false` | Include offscreen elements |
| `max_depth` | int | `15` | Maximum tree traversal depth |
| `max_elements` | int | `200` | Cap on interactive elements collected per snapshot; traversal stops once reached. `0` disables |
| `timeout` | int | `10` | Global wall-clock budget for one traversal across all windows (seconds). `0` disables |

### [clipboard]

Clipboard operation settings.

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `method` | str | `"system"` | Default method: `system` (PowerShell API) or `keyboard` (Ctrl+C/V) |

### [input]

DoS bounds and post-op verification for mouse/keyboard automation. All three keys are settable via `config set input.<key> <value>`.

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `type_text_len` | int | `10000` | Max text length accepted by `type` / `multi_edit` (over-limit is rejected with `INVALID_PARAMS`). `type` sleeps per character, so an oversized string would block the single serial worker for hours |
| `key_hold_seconds` | int | `60` | Max `--hold` duration in seconds for `key_down`. Over-limit is rejected at the handler edge |
| `verify` | bool | `true` | Toggle the post-op UIA hit-test run after `click`/`dblclick`/`hover`/`move`/`drag` (and `type`'s field-value read; returns the hit element + focused window, or slider % on drag). Millisecond-level check, far cheaper than a follow-up `snapshot`. Turn off for raw speed; see the troubleshooting "Click Landed in the Wrong Spot" note for the recovery flow when verify disagrees with intent |

### [wait]

Wait command polling settings.

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `poll_interval` | float | `0.5` | Default polling interval in seconds for `wait` element/text modes |
| `max_seconds` | int | `300` | DoS cap on the daemon-side `wait` / `wait_for` handlers (the ones `batch` calls) — an unbounded value would sleep on the single serial STA worker (the submit timeout only reports `WAIT_TIMEOUT`, it can't interrupt the sleep). The CLI `wait`/`wait-for` poll `wait_check`/`wait_for_check` client-side with their own `--timeout` and aren't bound by this |

### [audit]

Destructive-op audit log (the inverse of telemetry — default-on, low-frequency / high-harm ops). Records the target of `fs_delete` / `fs_move` / `process_kill` / `window_close` / registry set|delete / `system_shutdown` / `system_restart` / `sleep` to `~/.agent-windows/logs/audit.log`.

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `enabled` | bool | `true` | Toggle the destructive-op audit log. Operators who want it off set this to `false` |

> The HMAC key for audit entries lives under `[audit] key` but is **not** in the settable schema — it is lazily generated at runtime and stored DPAPI-encrypted (like `mcp.auth_key`). Do not edit it by hand.

### [shell]

Execution policy for the `shell` command.

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `blacklist` | str \| list[str] | `[]` | Case-insensitive regex pattern(s). A `shell` command matching any pattern is rejected with `PERMISSION_DENIED` (-1004) before execution. Empty disables filtering. A single string or a list of strings. Best-effort (PowerShell aliases/encodings can bypass). |
| `timeout` | int | `30` | Execution timeout in seconds. Used when a `shell` call omits an explicit `--timeout`/`timeout`. A timeout raises `WAIT_TIMEOUT` (-1003), not a generic internal error. |
| `max_output_bytes` | int | `1048576` (1 MiB) | Cap on combined stdout+stderr in raw bytes. stdout/stderr are drained incrementally and the subprocess is TERMINATED once the running byte total crosses the cap — bounding daemon memory (so a `1..1e6 \| %{ Write-Output ("X"*1000) }` runaway can't OOM the single-threaded daemon within the timeout) AND freeing the worker as soon as the cap is hit (instead of holding it for the full timeout). Output up to the cap is returned with stdout priority and an `[output truncated: command terminated after reaching M bytes (produced N)]` marker. `0` = explicit opt-out (no bound, no terminate — legacy behavior; memory unbounded by choice). The audit log is unaffected (it records commands, not output). |
| `output_encoding` | str | `""` | Output decode encoding. Empty (default) = UTF-8: the command is prefixed with a UTF-8 setup snippet so PowerShell 5.1 emits UTF-8 (not the system codepage) and CJK/other non-ASCII output decodes cleanly. Set to e.g. `'gbk'`/`'cp1252'` only if a native exe ignores the UTF-8 request and emits its legacy codepage — in that case the prefix is skipped and Python decodes the raw codepage. |

Every `shell` execution attempt — allowed or blocked — is recorded with the full, untruncated command in `~/.agent-windows/logs/shell_audit.log`. The `logs/` directory is owner-only via an inheritable ACL, so these records (which may contain secrets passed inline) are not readable by other users.

Setting the blacklist:

```bash
# Single pattern — works with `config set` (which only handles scalars):
agent-windows config set shell.blacklist "Remove-Item"

# Multiple patterns — edit config.toml directly into a TOML array:
#   [shell]
#   blacklist = ["Remove-Item", "Format-Volume", "shutdown"]
```

### [daemon]

Daemon process settings.

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `idle_timeout_minutes` | int | `30` | Auto-stop daemon after N minutes of inactivity |

### [ipc]

Client-side read timeout — prevents the CLI from hanging if the daemon is unresponsive.

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `read_timeout_seconds` | int | `150` | Seconds the client waits for a daemon response before raising `ConnectionError`. Must be long enough for the slowest commands |

### [telemetry]

Local-only tool-call telemetry.

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `enabled` | bool | `false` | When true, each tool call (source `mcp`\|`cli`, tool name, success/failure, `duration_ms`) is appended as one line to `~/.agent-windows/logs/telemetry.log`. Zero network, zero new deps. Opt in via `--telemetry`, the `AGENT_WINDOWS_TELEMETRY` env var, or this key. |

### [mcp] · [security] · [tools]

These sections configure the **MCP server's** HTTP transport, access controls, and startup tool filtering. They are HTTP/remote-access specific and documented in full in **[MCP.md](MCP.md)** (security model, flags, env vars, and ready-to-paste config). Summary of the keys:

| Section | Keys | Purpose |
|---------|------|---------|
| `[mcp]` | `transport`, `host`, `port`, `auth_key` (DPAPI-encrypted), `ssl_certfile`, `ssl_keyfile`, `allow_insecure_remote`, `stateless_http` | `mcp serve` transport/bind/TLS/auth defaults; `allow_insecure_remote` opts into a bare non-loopback bind; `stateless_http` runs streamable-http statelessly |
| `[security]` | `ip_allowlist`, `cors_origins` | HTTP-only access controls (both default `[]` = no restriction beyond the auth key/loopback guard) |
| `[tools]` | `include`, `exclude` | Startup tool filtering (`include` = explicit whitelist via `--tools`; `exclude` removes via `--exclude-tools`; both default `[]` = all tools) |

> These keys are arrays/booleans that `config set` (scalar-only) can't write — set them via the flags, `AGENT_WINDOWS_*` env vars, or by editing `config.toml` into TOML arrays. See MCP.md for the flag→env→config precedence and worked examples.

## Environment Variables

| Variable | Overrides | Description |
|----------|-----------|-------------|
| `AGENT_WINDOWS_VLM_API_KEY` | `vlm.api_key` | Vision model API key (never written to disk) |
| `AGENT_WINDOWS_SCREENSHOT_DIR` | Screenshot directory | Override screenshot storage location |
| `AGENT_WINDOWS_DEBUG` | — | Enable debug logging. The `--debug` flag just sets this env var; the spawned daemon inherits it. Truthy: `1`/`true`/`yes`/`on` |
| `AGENT_WINDOWS_TELEMETRY` | `telemetry.enabled` | Enable per-tool timing telemetry (writes to `~/.agent-windows/logs/telemetry.log`). The `--telemetry` flag and `[telemetry] enabled` config key are equivalent |
| `AGENT_WINDOWS_PYTHON` | — | **Launcher-only.** Overrides the Python interpreter the Rust launcher uses to spawn the daemon (default: `python` on `PATH`). Ignored by the Python CLI/daemon process itself |

Priority order: **CLI args > env vars > config file > defaults**

> MCP-server-scoped vars (`AGENT_WINDOWS_TOOLS`, `AGENT_WINDOWS_EXCLUDE_TOOLS`, `AGENT_WINDOWS_MCP_*`, etc.) are documented in **[MCP.md](MCP.md)** alongside the `[mcp]`/`[security]`/`[tools]` keys.

## Daemon Management

### Auto-Start

The daemon starts automatically on the first CLI call that requires it. No manual start needed for normal usage.

### Manual Control

```bash
agent-windows daemon start     # Force-start daemon
agent-windows daemon stop      # Graceful shutdown
agent-windows daemon status    # Check PID, version, pipe status
```

### Behavior

- **Config hot-applies**: The daemon re-reads the config file on every command (via its mtime), so `config set` takes effect on the **next command** — no daemon restart needed. The only exception is `daemon.idle_timeout_minutes` (read once at startup); change it then restart the daemon.
- **Idle Timeout**: Stops automatically after 30 minutes of no requests (configurable via `daemon.idle_timeout_minutes`).
- **Version Check**: `daemon status` warns if daemon version doesn't match CLI version. Stale daemons are auto-restarted on version mismatch.
- **`--no-daemon`**: Bypass daemon entirely. Runs commands in-process. Useful for debugging.
- **Serial Execution**: The daemon runs one command at a time. A single agent issues commands sequentially, so this costs nothing in normal use. It only matters when multiple agents share one daemon — they serialize against each other, so a slow `shell` or vision call blocks everyone else. For isolation, run separate daemon instances (separate user accounts or machines — the pipe path is per-user).

## Data Directory

Default location: `~/.agent-windows/`

Contents:

| Path | Description |
|------|-------------|
| `config.toml` | Configuration file |
| `daemon.pid` | Daemon state (PID, pipe path) |
| `daemon.version` | Daemon version for mismatch detection |
| `screenshots/` | Captured screenshots |
| `prompts/` | Custom grounding prompt files |
| `logs/` | Daemon log files (`daemon.log`) and the full-command `shell_audit.log`. Owner-only via an inheritable DACL: the directory and every file in it are readable only by the current user, so recorded commands (which may carry secrets) aren't exposed to other accounts on a shared host. |

## Screenshot Storage

- Default directory: `~/.agent-windows/screenshots/`
- Naming: `{command}-{timestamp}.png` (e.g., `screenshot-20260612-143022.png`)
- Auto-cleanup runs every 10 minutes (configurable):
  - Deletes screenshots older than `max_screenshot_age_days` (default 7)
  - Deletes oldest screenshots when directory exceeds `max_screenshot_dir_mb` (default 500 MB)
- `agent-windows cleanup` triggers immediate cleanup
- Custom path via `AGENT_WINDOWS_SCREENSHOT_DIR` env var

## Configuration Example

A typical config for development:

```toml
[vlm]
enabled = true
engine = "openai"
model = "gpt-4o-mini"
api_key = ""                         # use env var instead

[screenshot]
backend = "auto"
max_width = 1920
max_height = 1080

[tree]
max_depth = 15
max_elements = 200
timeout = 10

[clipboard]
method = "system"

[input]
type_text_len = 10000                 # max chars for type/multi_edit (DoS bound)
key_hold_seconds = 60                 # max --hold for key_down
verify = true                         # post-op UIA hit-test on click/hover/move/drag

[wait]
poll_interval = 0.5
max_seconds = 300                     # DoS cap on daemon-side wait/wait_for (batch); CLI waits use their own --timeout

[audit]
enabled = true                        # destructive-op audit log (logs/audit.log)
# key = "..."                         # auto-generated, DPAPI-encrypted — do not set by hand

[shell]
blacklist = []                        # regex patterns to block before execution
timeout = 30                          # seconds (overridable per-call via --timeout)
max_output_bytes = 1048576            # 1 MiB default; 0 = no cap. Command is terminated past the cap
output_encoding = ""                  # "" = UTF-8 (auto); else e.g. "gbk"

[daemon]
idle_timeout_minutes = 30

[ipc]
read_timeout_seconds = 150

[telemetry]
enabled = false                        # local-only; logs to logs/telemetry.log

# [mcp] / [security] / [tools] — HTTP transport, access controls, tool filtering.
# See MCP.md for these sections (auth_key, ssl_*, allow_insecure_remote,
# stateless_http, ip_allowlist, cors_origins, tools.include/exclude).
```
