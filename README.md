# agent-windows

> Abandoned, because most of models support vision for now.

Windows Computer-Use CLI for AI Agents. Provides desktop screenshot, UIA accessibility tree traversal, visual coordinate grounding, and mouse/keyboard automation through a fast Client-Daemon architecture.

## Highlights
- **Accessibility tree + visual grounding.** Two pixel-precise ways to locate element coordinates; the accessibility tree needs no vision model at all.
- **Vision for non-vision agents.** For agents whose model (DeepSeek, GLM, …) lacks multimodal input — screen reading is offloaded to any cheap/free/local vision model you configure. Only coordinates move through context, not whole screenshots — saving tokens and cost.
- **Fast Rust CLI.** ~10x faster than a traditional Python CLI.

## Install

Requires [uv](https://docs.astral.sh/uv/) (the Python package runner used below). Install it first if you don't have it:

```bash
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

Then install agent-windows:

```bash
uv tool install agent-windows
```

Or a one-off trial (runs without installing):

```bash
uvx agent-windows observe
```

> **Prefer `uv tool install` over `uvx` for regular use.** `uvx` runs from a cache that can be silently refreshed, triggering unwanted daemon restarts when its version drifts from the CLI's. See [troubleshooting](skills/agent-windows/references/troubleshooting.md#daemon-keeps-restarting-uvx-version-drift).

From source (a checkout of this repo):

```bash
uv sync
uv run agent-windows ...
```

To rebuild the native launcher from source (needs Rust + MSVC + Windows SDK):

```bash
cd src/launcher && build.cmd build --release   # stages prebuilt/agent-windows.exe (gitignored)
```


## Quick Start

```bash
# Initialize config
agent-windows config init

# Capture screenshot
agent-windows screenshot

# Get UIA tree
agent-windows snapshot

# Combined observation (snapshot + screenshot)
agent-windows observe

# Visual grounding (locate element by description)
agent-windows ground "the taskbar"

# Mouse/keyboard
agent-windows click 500 300
agent-windows type "Hello World" --enter
agent-windows shortcut ctrl+c

# System
agent-windows app list
agent-windows shell "Get-Process | Select-Object -First 5"
agent-windows batch --file commands.json
```

## Architecture

```
agent-windows.exe (native Rust launcher, ~12ms)
    │
    ▼ Named Pipe IPC (JSON-RPC 2.0)
    │
Python daemon (persistent, COM STA worker)
```

- **Native launcher** (`agent-windows.exe`): a Rust binary that parses argv and forwards JSON-RPC requests to the daemon. Source: `src/launcher/`. It also delegates `config` to the Python CLI (forwarding argv verbatim), so config is usable as `agent-windows config ...`. The Python CLI remains available directly as `agent-windows-python` for `--no-daemon` mode.
- **Daemon**: Persistent Python process holding heavy imports (comtypes, uia, Pillow). Handlers are organized by domain in `daemon/handlers/`.
- **IPC**: Named Pipe with per-user DACL security. JSON-RPC 2.0 protocol.
- **Auto-start**: First CLI call starts daemon automatically. Idle timeout (30min) stops it.
- **Serial execution**: one command at a time — UIA/COM is apartment-bound and `SendInput` mutates global GUI state, so concurrent commands would race. Only matters when multiple agents share one daemon (they serialize against each other); for isolation, run separate daemons (one user account each — the pipe path is per-user).

## Commands

Full flags and examples live in the [Command Reference](skills/agent-windows/references/commands.md); for when-to-use guidance see the [Agent Skill](skills/agent-windows/SKILL.md). Run `agent-windows --help` for a quick list.

### Observation
| Command | Description |
|---------|-------------|
| `observe` | Combined observation (snapshot + screenshot; `--annotate` overlays `@eN` boxes) |
| `snapshot` | UIA accessibility tree; `--window`/`--active` scope to one window |
| `list-displays` | List displays (index, primary, resolution, rect, work area, device) for `-d N` |
| `screenshot` | Screen capture; crop, annotate `@eN` boxes, overlay grid |
| `ground` | Visual coordinate grounding (`--screenshot [PATH]` marks the hit; `--single-pass`: faster, lower precision) |
| `detect` | Multi-target bounding box detection |
| `find-image` | Locate a saved template image on screen — deterministic, no vision model |
| `pixel-at` | Read the color at a screen point; `--match` judges state by color — deterministic |
| `ask-vision` | Free-form visual Q&A |
| `highlight` | Highlight a region on a screenshot |
| `diff` | Pixel-level screenshot diff |
| `snapshot-diff` | Diff a stored UIA-tree baseline vs now (added/removed/changed elements) |
| `get-text` | Get text content of a UIA element |
| `is visible/enabled` | Check element state |

### Input
| Command | Description |
|---------|-------------|
| `click` | Mouse click (coordinates or `@eN`); `--background` clicks without stealing focus (UIA pattern; `-1007` if unsupported) |
| `type` | Type text (Unicode direct input) |
| `shortcut` | Keyboard shortcut (e.g. `ctrl+c`) |
| `key-down` / `key-up` | Raw key press/release; `--hold N` auto-releases, else pair them |
| `expand` / `collapse` | Expand/collapse an element (`@eN`) — ComboBox/TreeItem/Menu |
| _also_ | `dblclick`, `hover`, `scroll`, `move`, `drag`, `mouse-down`, `mouse-up` |

### System
| Command | Description |
|---------|-------------|
| `app launch` | Launch app by name (Start-Menu friendly name, UWP/Store apps), exe, or path |
| `app switch` | Switch to a window |
| `app resize` | Resize/reposition a window |
| `app list` | List visible windows |
| `window minimize/maximize/restore/close/focus` | Window control |
| `window enable` | Re-enable a window frozen by an interrupted `--background` click |
| `shell` | Run a PowerShell command (UTF-8 output) |
| `fs read/write/list/copy/move/delete/search/info` | Filesystem operations |
| `clipboard get/set` | Clipboard access (`--keyboard` simulates Ctrl+C/V) |
| `process list` | List running processes |
| `process kill` | Kill a process (graceful or `--force`) |
| `notify` | Windows toast notification |
| `registry get/set/delete/list` | Windows Registry operations (typed values) |
| `lock` | Lock the workstation (Win+L) |
| `sleep` | Suspend the system to sleep (S3) |
| `shutdown` | Shut down (`--delay N` 0=immediate max 600, `--force` skip save dialogs) |
| `restart` | Restart (`--delay N`, `--force`) |
| `cleanup` | Clean up old/oversized screenshots |
| `wait` | Wait for a duration, element, or text |
| `wait-for` | Wait until a UI predicate holds |
| `batch` | Execute multiple commands in one IPC round-trip |
| `multi-select` | Click multiple targets (Ctrl held by default) |
| `multi-edit` | Type into multiple fields in one call |

### Daemon & Config
| Command | Description |
|---------|-------------|
| `daemon start/stop/status` | Daemon management |
| `ping` | Daemon health check (single IPC round-trip) |
| `config init/show/set/doctor` | Configuration (delegated to the Python CLI) |
| `audit verify` | Verify the destructive-op audit log integrity (CLI-only) |
| `mcp serve/auth/install/uninstall` | MCP server (delegated to the Python CLI) |

### Global Options
| Flag | Description |
|------|-------------|
| `--no-daemon` | Direct execution (bypass daemon, for debugging) |
| `--format text\|json` | Output format |
| `--debug` | Verbose logging |
| `--config <path>` | Alternate config.toml (default `~/.agent-windows/config.toml`). Propagated to the daemon; reads *and* writes (`config set`/`mcp auth`) target this file. State files (pid/lock/logs) stay in the default location. |

## MCP Server

agent-windows also ships a complete [MCP](https://modelcontextprotocol.io/) server (built with [FastMCP](https://github.com/jlowin/fastmcp)), exposing the automation commands as MCP tools. Any MCP-capable client — Claude Desktop, Cursor, Gemini CLI, etc. — can drive Windows through it. **This project is CLI-first; MCP is an alternative interface.** The full guide — client install configs, remote HTTP access, auth, transports, and the tool catalog — lives in **[MCP.md](MCP.md)**.

The 30-second version (local stdio, e.g. for Claude Desktop):

```bash
agent-windows mcp serve          # auto-starts the daemon on the first tool call
```

```json
{
  "mcpServers": {
    "agent-windows": { "command": "agent-windows", "args": ["mcp", "serve"] }
  }
}
```

> Requires the `mcp` extra: `uv tool install "agent-windows[mcp]"` (the bare `uv tool install agent-windows` pulls core deps only); from source run `uv sync --extra mcp`.

## Agent Skill

The repo ships a ready-made agent skill at [`skills/agent-windows/SKILL.md`](skills/agent-windows/SKILL.md) (with `references/`). It's the usage guide for any agent driving the CLI: the `observe → find @eN → act → verify` core loop, the `@eN` selector system and its staleness rules, the snapshot/ground/ask-vision decision matrix, per-error-code recovery, and ready-to-run workflows.

**Install it for any CLI-driven agent.** Without it the agent only has `--help` and must rediscover the loop, selector lifecycle, and error handling on its own. **Skip it if the agent uses the [MCP server](#mcp-server)** — MCP clients get the tools directly and never run the CLI.

```bash
# Claude Code (macOS / Linux / Git Bash):
cp -r skills/agent-windows ~/.claude/skills/
# Claude Code (PowerShell, Windows):
Copy-Item -Recurse skills/agent-windows $HOME/.claude/skills/
```

Project-scoped instead of user-wide: copy into `.claude/skills/`. For other agent runtimes, point the agent at `skills/agent-windows/SKILL.md` as a skill or system-prompt include.

## Configure a Vision Model (optional, recommended)

`ground`, `detect`, and `ask-vision` need a multimodal model to read the screen. Configure one with `config set`:

```bash
# 1. API key (or set AGENT_WINDOWS_VLM_API_KEY env var instead)
agent-windows config set vlm.api_key sk-xxxxxxxx

# 2. Engine: openai (default) / anthropic / gemini
agent-windows config set vlm.engine openai

# 3. Model (a cheap multimodal model is enough)
agent-windows config set vlm.model gpt-4o-mini

# 4. Verify
agent-windows config doctor
```

> For non-OpenAI endpoints, also set `vlm.api_base` (e.g. `agent-windows config set vlm.api_base https://your-endpoint/v1`).

## Configuration

Config file: `~/.agent-windows/config.toml` (auto-created with owner-only permissions)

> **Path resolution**: relative `[mcp] ssl_certfile` / `ssl_keyfile` paths resolve against the **config file's directory**, not the working directory. Absolute and `~`-prefixed paths are used as-is.

> **Security**: `vlm.api_key` is **DPAPI-encrypted at rest** (current-user scope); the config file is owner-only ACL'd. Set it via `config set vlm.api_key sk-...` or the `AGENT_WINDOWS_VLM_API_KEY` env var (which takes priority and is never written to disk). Keys are masked in `config show`.

> **When changes take effect**: the daemon re-reads config on every command (mtime-checked), so `config set` applies to the **next command** — no restart. Exception: `daemon.idle_timeout_minutes` is read once at startup.

```toml
[vlm]
enabled = true
engine = "openai"       # openai / anthropic / gemini (+ doubao / gemini_compatible → OpenAI-compatible)
model = "gpt-4o-mini"
api_base = "https://api.openai.com/v1"
api_key = ""             # or AGENT_WINDOWS_VLM_API_KEY env var
timeout = 30
image_format = "JPEG"   # JPEG or PNG
jpeg_quality = 85        # JPEG quality (1-95), ignored when image_format = PNG

[screenshot]
backend = "auto"         # auto / dxcam / mss / pillow
max_width = 1920
max_height = 1080
resampling = "BILINEAR"  # BILINEAR / LANCZOS / NEAREST

[tree]
max_depth = 15         # max traversal depth
max_elements = 200     # cap on interactive elements per snapshot (0 disables)
timeout = 10           # global traversal budget, seconds (0 disables)

[clipboard]
method = "system"      # "system" (PowerShell API) or "keyboard" (simulate Ctrl+C/V)

[shell]
timeout = 30             # execution timeout, seconds (overridable per-call via --timeout)
max_output_bytes = 1048576  # stdout+stderr cap (1 MiB); 0 = no cap; command killed on overflow
output_encoding = ""     # "" = UTF-8 (auto); else e.g. "gbk" for legacy-codepage exes
```

### Environment variables

Every option below can be set as an env var instead of (or in addition to) the config file. Precedence is **flag > env var > config** everywhere.

| Variable | Area | Purpose |
|---|---|---|
| `AGENT_WINDOWS_VLM_API_KEY` | VLM | Vision-model API key; priority over `[vlm] api_key`, never written to disk. |
| `AGENT_WINDOWS_SCREENSHOT_DIR` | Screenshot | Override screenshot dir (default `~/.agent-windows/screenshots/`). |
| `AGENT_WINDOWS_DEBUG` | Debug | Set by `--debug`; modules read via `is_debug()`, daemon inherits it. |
| `AGENT_WINDOWS_TELEMETRY` | Telemetry | `1` → local-only tool-call logging (`logs/telemetry.log`, no network). |
| `AGENT_WINDOWS_PYTHON` | Launcher | Explicit Python interpreter for the daemon. |
| `AGENT_WINDOWS_E2E` | Testing | `1` → enable e2e daemon smoke tests (`@pytest.mark.e2e`). |
| MCP server vars | MCP | Same names as the `mcp serve` flags but `AGENT_WINDOWS_`-prefixed: `AGENT_WINDOWS_ALLOW_INSECURE_REMOTE`, `AGENT_WINDOWS_IP_ALLOWLIST`, `AGENT_WINDOWS_CORS_ORIGINS`, `AGENT_WINDOWS_TOOLS`, `AGENT_WINDOWS_EXCLUDE_TOOLS`, `AGENT_WINDOWS_STATELESS_HTTP`, `AGENT_WINDOWS_FILE_TRANSFER`, `AGENT_WINDOWS_MAX_UPLOAD_MB`, `AGENT_WINDOWS_OAUTH_CLIENT_ID`/`SECRET` — see [MCP.md](MCP.md#http-security--scaling-options). |

## Requirements

- Windows 8.1 / 10 / 11
- Python 3.13+
- Dependencies: typer, comtypes, pywin32 (≥306; ≥308 recommended), Pillow, mss, openai, toml, numpy
- Optional: fastmcp (≥3.0, for the MCP server — `uv tool install "agent-windows[mcp]"` / `uv sync --extra mcp`), dxcam, anthropic, google-genai

> **pywin32**: wheels ≥308 self-configure their DLLs via a `.pth` bootstrap, so `uv tool install` / `uvx` works out of the box. On older pywin32 or manual envs missing `pythoncom3xx.dll` / `pywintypes3xx.dll`, run `python Scripts\pywin32_postinstall.py -install`.

## License

agent-windows is licensed under the **MIT License** (see the root `LICENSE`).

The vendored `uia` module under `src/agent_windows/uia/` is derived from [yinkaisheng/Python-UIAutomation-for-Windows](https://github.com/yinkaisheng/Python-UIAutomation-for-Windows), which is **Apache License 2.0** — see `src/agent_windows/uia/LICENSE-APACHE.txt` and `NOTICE` in that directory. Original copyright notices are retained in each derived file. Some other patterns are adapted from [Windows-MCP](https://github.com/CursorTouch/Windows-MCP) (MIT).

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Report security issues via [SECURITY.md](SECURITY.md).
