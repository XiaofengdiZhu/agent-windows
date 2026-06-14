---
name: configuration
description: "Configuration reference for agent-windows: config file structure, all settings with defaults, environment variables, daemon management, and screenshot storage. Read when you need to customize behavior or troubleshoot config issues."
allowed-tools: Bash(agent-windows:*)
---

# Configuration Reference

## Config File

Path: `~/.agent-windows/config.toml`

### Management Commands

```bash
agent-windows config init              # Create default config file
agent-windows config show              # Print full config with file path
agent-windows config set key value     # Set a config value (dotted key notation)
```

### Example

```bash
agent-windows config set grounding.model gpt-4o
agent-windows config set tree.max_depth 50
agent-windows config set wait.poll_interval 0.3
```

## Configuration Sections

### [grounding]

Vision model settings for `ground`, `detect`, and `ask-vision`.

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `enabled` | bool | `true` | Enable/disable vision features |
| `engine` | str | `"openai"` | Vision engine: `openai`, `anthropic`, or `gemini` |
| `model` | str | `"gpt-4o-mini"` | Default model for vision calls |
| `api_base` | str | `"https://api.openai.com/v1"` | API endpoint URL |
| `api_key` | str | `""` | API key. **Encrypted at rest** with Windows DPAPI (current-user scope); set via `config set grounding.api_key sk-...` and it is transparently decrypted on load. Legacy plaintext values still work and are re-encrypted on next write. (Or use the `AGENT_WINDOWS_GROUNDING_API_KEY` env var) |
| `timeout` | int | `30` | Request timeout in seconds |
| `width` | int | `1000` | Screenshot resize width for model input |
| `height` | int | `1000` | Screenshot resize height for model input |
| `crop_size` | int | `400` | Crop width for fine-pass in `ground` |
| `crop_height` | int | `300` | Crop height for fine-pass in `ground` |
| `max_drift` | int | `40` | Max pixel drift for accepting fine-pass result |
| `image_format` | str | `"JPEG"` | Encoding format for images sent to the vision API: `JPEG` or `PNG`. JPEG is ~5-10x smaller with no loss the model perceives. |
| `jpeg_quality` | int | `85` | JPEG quality (1-95) for vision API images. Ignored when `image_format = PNG`. |

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
| `resampling` | str | `"BILINEAR"` | Downscale filter for saved screenshots: `BILINEAR` (fast, default), `LANCZOS` (highest quality), or `NEAREST` |
| `max_screenshot_age_days` | int | `7` | Auto-delete screenshots older than this |
| `max_screenshot_dir_mb` | int | `500` | Auto-cleanup when screenshot dir exceeds this size |
| `cleanup_interval_minutes` | int | `10` | How often to run auto-cleanup check |

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

### [wait]

Wait command polling settings.

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `poll_interval` | float | `0.5` | Default polling interval in seconds for `wait` element/text modes |

### [shell]

`shell` command execution policy (M-05).

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `blacklist` | str \| list[str] | `[]` | Case-insensitive regex pattern(s). A `shell` command matching any pattern is rejected with `PERMISSION_DENIED` (-1004) before execution. Empty disables filtering. A single string or a list of strings. Best-effort (PowerShell aliases/encodings can bypass) — pair with the audit log for a complete record. |

Every `shell` execution attempt — allowed **or** blocked — is also recorded with the **full, untruncated** command in `~/.agent-windows/logs/shell_audit.log`, regardless of the blacklist setting. This is the forensic record (the daemon's own log truncates commands at 500 chars). The `logs/` directory is owner-only via an inheritable ACL (M-01), so these full-command records (which may contain secrets passed inline) are not readable by other users on a shared host.

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

Named-pipe client read timeout (H-02). The client uses overlapped I/O so a hung or crashed daemon can never block the CLI forever.

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `read_timeout_seconds` | int | `150` | Per-operation seconds the client waits for daemon progress before raising `ConnectionError`. Applied per overlapped read/write, not a hard ceiling on total response time (a large response streams across multiple intervals). Must exceed the daemon's slowest legitimate operation (grounding two-pass ≈ 60s) |

## Environment Variables

| Variable | Overrides | Description |
|----------|-----------|-------------|
| `AGENT_WINDOWS_GROUNDING_API_KEY` | `grounding.api_key` | Vision model API key |
| `AGENT_WINDOWS_SCREENSHOT_DIR` | Screenshot directory | Override screenshot storage location |

Priority order: **CLI args > env vars > config file > defaults**

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

- **Named Pipe**: Daemon listens on `\\.\pipe\agent-windows` for JSON-RPC requests.
- **COM STA**: Daemon runs on a COM Single-Threaded Apartment thread for UIA access.
- **Idle Timeout**: Stops automatically after 30 minutes of no requests (configurable).
- **Version Check**: `daemon status` warns if daemon version doesn't match CLI version. Stale daemons are auto-restarted on version mismatch.
- **`--no-daemon`**: Bypass daemon entirely. Runs commands in-process. Useful for debugging.

### Three-Thread Model

1. **I/O Thread**: Named pipe listener, handles incoming requests.
2. **COM STA Worker**: Executes UIA operations, screenshots, input simulation.
3. **Idle Monitor**: Tracks inactivity and triggers shutdown.

### Concurrency: Serial by Design

The daemon processes **exactly one command at a time**. This is intentional, not a limitation to fix:

- The named pipe is single-instance — only one client can connect at once.
- The I/O thread blocks until each command finishes before accepting the next connection.
- All commands run on the single COM STA worker thread, one at a time.

**Why serial is correct:** UIA/COM objects are apartment-bound to the STA thread that created them, so all UIA calls must run there. Input simulation (`SendInput`) mutates global GUI state — two concurrent click/type commands would interleave into garbage. Serializing keeps GUI automation deterministic.

**Practical impact:** For the normal workflow — one agent issuing commands sequentially — this costs nothing. The client is naturally serial too: each `send_to_daemon` call sends one request, blocks on the response, then reconnects for the next. The only situation it bites is **multiple agents sharing one daemon**: they serialize against each other, so a slow `shell` (up to ~30s) or a vision API call (2-10s) blocks everyone else.

Concurrent multi-client use is **not supported**. For isolation between agents, run separate daemon instances (separate user accounts or machines) — the pipe path is per-user.

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
| `logs/` | Daemon log files (`daemon.log`) and the full-command `shell_audit.log` (M-05). Owner-only via an inheritable DACL (M-01): the directory and every file in it are readable only by the current user, so recorded commands (which may carry secrets) aren't exposed to other accounts on a shared host. |

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
[grounding]
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

[wait]
poll_interval = 0.5

[daemon]
idle_timeout_minutes = 30
```
