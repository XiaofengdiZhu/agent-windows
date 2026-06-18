# agent-windows

Windows Computer-Use CLI for AI Agents. Provides desktop screenshot, UIA accessibility tree traversal, visual coordinate grounding, and mouse/keyboard automation through a fast Client-Daemon architecture.

## Install

Recommended — adds `agent-windows` to your PATH as a persistent command:

```bash
uv tool install agent-windows
```

One-off trial (runs without installing):

```bash
uvx agent-windows observe
```

> **Why `uv tool install` over `uvx` for regular use:** the daemon is long-lived (30 min idle timeout) and auto-restarts when its version no longer matches the CLI's. `uv tool install` pins both versions together; `uvx` runs from a cache that can be refreshed (new release, cache expiry, `uvx -U`) and silently bump the CLI, which triggers a daemon restart that drops any in-flight UIA state. If you see the daemon restarting on every few calls under `uvx`, that's the cause — install it with `uv tool install` instead.

From source (a checkout of this repo):

```bash
uv sync
uv run agent-windows ...
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
# Single-pass: one API call, ~2x faster, lower precision (overrides config grounding.single_pass)
agent-windows ground "the taskbar" --single-pass

# Detect multiple targets (returns bounding boxes)
agent-windows detect "PDF file icons on the desktop"

# Visual Q&A (custom prompt + screenshot)
agent-windows ask-vision "Which windows are open? Is there an error dialog?"

# Mouse/keyboard
agent-windows click 500 300
agent-windows type "Hello World" --enter
agent-windows shortcut ctrl+c
agent-windows scroll 500 300 --direction down --amount 5

# System
agent-windows app list
agent-windows shell "Get-Process | Select-Object -First 5"
agent-windows fs list C:\\Users
agent-windows clipboard get
agent-windows batch --file commands.json
```

## Architecture

```
CLI (Typer) → Named Pipe IPC → Daemon (COM STA)
```

- **CLI**: Lightweight Typer entry point. Parses args, sends JSON-RPC to daemon.
- **Daemon**: Persistent process holding heavy imports (comtypes, uia, Pillow). Three-thread model: I/O thread (overlapped I/O), COM STA worker thread, idle monitor. Handlers are organized by domain in `daemon/handlers/`.
- **IPC**: Named Pipe with per-user DACL security. JSON-RPC 2.0 protocol.
- **Auto-start**: First CLI call starts daemon automatically. Idle timeout (30min) stops it. Graceful shutdown via overlapped I/O.
- **Caching**: UIA tree traversal (2s TTL), config loading (mtime-based), API clients, and screenshot backends are all cached for performance.
- **Concurrency (serial by design)**: The daemon handles **one command at a time** — the named pipe is single-instance and the I/O thread blocks until each command finishes. This is intentional, not a limitation to fix: UIA/COM is STA-bound and `SendInput` mutates global GUI state, so concurrent commands would race and corrupt automation. A single client is naturally serial anyway (request → wait for response → reconnect), so it costs nothing in normal use. The one caveat: **multiple agents sharing one daemon serialize against each other** — a slow `shell` (~30s) or vision API call (2-10s) blocks the rest. Concurrent multi-client use is not supported; for isolation between agents, run separate daemon instances. Because the named-pipe path is per-user (`~/.agent-windows/`), the simplest isolation is **one Windows user account per agent** — each gets its own daemon, config, and screenshot directory with no contention. On a single account, stagger agent activity or accept the serialization.

## Commands

### Observation
| Command | Description |
|---------|-------------|
| `observe [--snapshot] [--screenshot] [--ground DESC]` | Combined observation |
| `snapshot` | UIA accessibility tree |
| `screenshot [-o PATH] [-s @eN] [--bbox X1,Y1,X2,Y2]` | Screen capture (optionally cropped to UIA element or region) |
| `ground DESCRIPTION [--single-pass]` | Visual coordinate grounding (`--single-pass`: 1 API call, ~2x faster, lower precision) |
| `detect DESCRIPTION [-i IMAGE] [--max-targets N]` | Multi-target bounding box detection |
| `ask-vision PROMPT [-i IMAGE] [-m MODEL]` | Free-form visual Q&A |
| `highlight X1 Y1 X2 Y2 [-l LABEL] [-c COLOR]` | Highlight region on screenshot |
| `diff BASELINE [-c COMPARE] [-t THRESHOLD]` | Pixel-level screenshot diff |
| `get-text SELECTOR` | Get text content of a UIA element |
| `is visible/enabled SELECTOR` | Check element state |

### Input
| `click X Y [--right] [--double]` | Mouse click |
| `dblclick X Y [--right]` | Double-click |
| `hover X Y` | Hover (trigger tooltips/highlights) |
| `mouse-down X Y [--button left\|right\|middle]` | Press and hold mouse button |
| `mouse-up X Y [--button left\|right\|middle]` | Release mouse button |
| `type TEXT [--at X,Y] [--enter] [--clear]` | Type text (Unicode direct input) |
| `scroll X Y [--direction up\|down] [--amount N]` | Scroll |
| `move X Y` | Move mouse |
| `drag X1 Y1 X2 Y2 [--right]` | Drag |
| `shortcut KEYS` | Keyboard shortcut (e.g. `ctrl+c`) |

### System
| Command | Description |
|---------|-------------|
| `app launch NAME [--args ...]` | Launch application |
| `app switch NAME` | Switch to window |
| `app resize NAME --width W --height H` | Resize window |
| `app list` | List visible windows |
| `shell COMMAND` | Run PowerShell command |
| `fs read/write/list PATH` | File operations |
| `clipboard get/set [--keyboard]` | Clipboard access (`--keyboard` simulates Ctrl+C/V) |
| `process list/kill` | Process management |
| `cleanup` | Clean up old/oversized screenshots |
| `wait SECONDS|SELECTOR [--text TEXT] [--timeout N] [--interval N]` | Wait for duration, element, or text |
| `batch JSON/--file PATH [--delay N] [--continue-on-error]` | Execute multiple commands in one IPC round-trip |

### Daemon & Config
| Command | Description |
|---------|-------------|
| `daemon start/stop/status` | Daemon management |
| `config init/show/set` | Configuration |

### Global Options
| Flag | Description |
|------|-------------|
| `--no-daemon` | Direct execution (bypass daemon, for debugging) |
| `--format text\|json` | Output format |
| `--debug` | Verbose logging |

## Configuration

Config file: `~/.agent-windows/config.toml` (auto-created with owner-only permissions)

> **Security**: The `grounding.api_key` is **encrypted at rest** with Windows DPAPI (current-user scope) and the file is restricted to your Windows account via an owner-only ACL. Decryption happens transparently on load — you only ever set the plaintext key. Legacy plaintext keys in an existing file still work and are encrypted on the next `config set`. API keys in `config show` output are automatically masked. For CI/CD or shared environments, use the `AGENT_WINDOWS_GROUNDING_API_KEY` environment variable — it takes priority over the config file and is never written to disk.

```toml
[grounding]
enabled = true
engine = "openai"       # openai / anthropic / gemini
model = "gpt-4o-mini"
api_base = "https://api.openai.com/v1"
api_key = ""             # or AGENT_WINDOWS_GROUNDING_API_KEY env var
timeout = 30
image_format = "JPEG"   # JPEG (smaller/faster, default) or PNG for vision API
jpeg_quality = 85        # JPEG quality (1-95), ignored when image_format = PNG

[screenshot]
backend = "auto"         # auto / dxcam / mss / pillow
max_width = 1920
max_height = 1080
resampling = "BILINEAR"  # downscale filter: BILINEAR (default) / LANCZOS / NEAREST

[tree]
max_depth = 15         # max traversal depth
max_elements = 200     # cap on interactive elements per snapshot (0 disables)
timeout = 10           # global traversal budget, seconds (0 disables)

[clipboard]
method = "system"      # "system" (PowerShell API) or "keyboard" (simulate Ctrl+C/V)
```

> **Performance**: Vision model input is resized with BILINEAR (not LANCZOS) and encoded as JPEG by default — together this cuts each grounding call's image-prepare + upload cost substantially with no loss the model can perceive. Saved screenshots likewise default to BILINEAR; set `screenshot.resampling = "LANCZOS"` if you need maximum fidelity.

## Desktop Automation Tips

### 1. Always switch window before input

Use `app switch` before any keyboard or mouse operation. Otherwise shortcuts may be sent to the wrong window — with unpredictable results (e.g. ESC in a terminal can kill the running process).

```bash
agent-windows app switch "WPS"
# now safe to click / type / shortcut
```

### 2. Prefer UIA tree over vision for element location

`snapshot` returns precise bounding rectangles for named UI elements. `ground` / `detect` use a vision model whose coordinates can be off by ~100 px (typically biased toward the left edge of text). Use vision tools as a supplement when UIA can't see an element.

| Tool | Precision | Best for |
|------|-----------|----------|
| `snapshot` | Pixel-exact | Named controls, dialogs, menus |
| `ground` / `detect` | ±~100 px | Custom-drawn UI that UIA can't capture |
| `ask-vision` | Descriptive only | Understanding icons, reading screen state |

### 3. Navigating deep menus with mixed tools

Some actions are buried in 2nd- or 3rd-level menus. Buttons may show only icons with no text. A practical approach:

1. Use `ask-vision` to describe what each icon does in the current panel.
2. Use `ground` / `detect` to get approximate coordinates for candidate buttons.
3. Cross-reference with `snapshot` (UIA tree) for any named elements in the same area.
4. Click and verify the result before proceeding to the next step.

## Requirements

- Windows 10/11
- Python 3.13+
- Dependencies: typer, comtypes, pywin32 (≥308), Pillow, mss, openai

> **pywin32 note**: modern pywin32 wheels (≥308) self-configure their DLLs via a `.pth` bootstrap at install time, so `uv tool install` / `uvx` works out of the box. If you ever hit `pythoncom3xx.dll` / `pywintypes3xx.dll` not found on an older pywin32 or a manually-managed environment, run `python Scripts\pywin32_postinstall.py -install` from your install prefix.

## License

Apache 2.0 (uia module vendored from [yinkaisheng/Python-UIAutomation-for-Windows](https://github.com/yinkaisheng/Python-UIAutomation-for-Windows) and [Windows-MCP](https://github.com/_ptrs/windows-mcp))
