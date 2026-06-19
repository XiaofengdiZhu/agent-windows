<!-- Reference doc — loaded on demand via the table in SKILL.md, not discovered as a standalone skill. -->

# Configuration Reference

## Config File

Path: `~/.agent-windows/config.toml`

### Management Commands

```bash
agent-windows-python config init              # Create default config file
agent-windows-python config show              # Print full config with file path
agent-windows-python config set key value     # Set a config value (dotted key notation)
agent-windows-python config doctor            # Check whether the vision model is configured
```

### Example

```bash
agent-windows-python config set grounding.model gpt-4o
agent-windows-python config set tree.max_depth 50
agent-windows-python config set wait.poll_interval 0.3
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
| `image_format` | str | `"JPEG"` | Encoding format for images sent to the vision API: `JPEG` or `PNG` |
| `jpeg_quality` | int | `85` | JPEG quality (1-95) for vision API images. Ignored when `image_format = PNG` |
| `single_pass` | bool | `false` | Skip the fine-pass API call in `ground`: one network round-trip instead of two (faster, lower cost) at the cost of click precision — coarse coordinate only, no sub-element refinement. Override per call with `ground ... --single-pass`. |

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

Execution policy for the `shell` command.

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `blacklist` | str \| list[str] | `[]` | Case-insensitive regex pattern(s). A `shell` command matching any pattern is rejected with `PERMISSION_DENIED` (-1004) before execution. Empty disables filtering. A single string or a list of strings. Best-effort (PowerShell aliases/encodings can bypass). |

Every `shell` execution attempt — allowed or blocked — is recorded with the full, untruncated command in `~/.agent-windows/logs/shell_audit.log`. The `logs/` directory is owner-only via an inheritable ACL, so these records (which may contain secrets passed inline) are not readable by other users.

Setting the blacklist:

```bash
# Single pattern — works with `config set` (which only handles scalars):
agent-windows-python config set shell.blacklist "Remove-Item"

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
