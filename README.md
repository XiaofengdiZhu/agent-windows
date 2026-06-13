# agent-windows

Windows Computer-Use CLI for AI Agents. Provides desktop screenshot, UIA accessibility tree traversal, visual coordinate grounding, and mouse/keyboard automation through a fast Client-Daemon architecture.

## Install

```bash
uv sync
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
agent-windows ground "任务栏"

# Detect multiple targets (returns bounding boxes)
agent-windows detect "桌面上的 PDF 文件图标"

# Visual Q&A (custom prompt + screenshot)
agent-windows ask-vision "屏幕上有哪些窗口？是否有错误弹窗？"

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
CLI (Typer, ~5ms) → Named Pipe IPC → Daemon (COM STA, ~20ms response)
```

- **CLI**: Lightweight Typer entry point. Parses args, sends JSON-RPC to daemon.
- **Daemon**: Persistent process holding heavy imports (comtypes, uia, Pillow). Three-thread model: I/O thread (overlapped I/O), COM STA worker thread, idle monitor. Handlers are organized by domain in `daemon/handlers/`.
- **IPC**: Named Pipe with per-user DACL security. JSON-RPC 2.0 protocol.
- **Auto-start**: First CLI call starts daemon automatically. Idle timeout (30min) stops it. Graceful shutdown via overlapped I/O.
- **Caching**: UIA tree traversal (2s TTL), config loading (mtime-based), API clients, and screenshot backends are all cached for performance.

## Commands

### Observation
| Command | Description |
|---------|-------------|
| `observe [--snapshot] [--screenshot] [--ground DESC]` | Combined observation |
| `snapshot` | UIA accessibility tree |
| `screenshot [-o PATH] [-s @eN] [--bbox X1,Y1,X2,Y2]` | Screen capture (optionally cropped to UIA element or region) |
| `ground DESCRIPTION` | Visual coordinate grounding |
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

> **Security**: API keys in `config show` output are automatically masked. Use the `AGENT_WINDOWS_GROUNDING_API_KEY` environment variable for CI/CD or shared environments — it takes priority over the config file.

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
max_depth = 30

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
- Dependencies: typer, comtypes, pywin32, Pillow, mss, openai

## License

Apache 2.0 (uia module vendored from [yinkaisheng/Python-UIAutomation-for-Windows](https://github.com/yinkaisheng/Python-UIAutomation-for-Windows) and [Windows-MCP](https://github.com/_ptrs/windows-mcp))
