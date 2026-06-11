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
```

## Architecture

```
CLI (Typer, ~5ms) → Named Pipe IPC → Daemon (COM STA, ~20ms response)
```

- **CLI**: Lightweight Typer entry point. Parses args, sends JSON-RPC to daemon.
- **Daemon**: Persistent process holding heavy imports (comtypes, uia, Pillow). Three-thread model: I/O thread, COM STA worker thread, idle monitor.
- **Auto-start**: First CLI call starts daemon automatically. Idle timeout (30min) stops it.

## Commands

### Observation
| Command | Description |
|---------|-------------|
| `observe [--snapshot] [--screenshot] [--ground DESC]` | Combined observation |
| `snapshot` | UIA accessibility tree |
| `screenshot [-o PATH]` | Screen capture |
| `ground DESCRIPTION` | Visual coordinate grounding |
| `ask-vision PROMPT [-i IMAGE] [-m MODEL]` | Free-form visual Q&A |
| Command | Description |
|---------|-------------|
| `click X Y [--right] [--double]` | Mouse click |
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
| `clipboard get/set` | Clipboard access |
| `process list/kill` | Process management |
| `wait SECONDS` | Wait |

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

Config file: `~/.agent-windows/config.toml`

```toml
[grounding]
enabled = true
engine = "openai"       # openai / anthropic / gemini
model = "gpt-4o-mini"
api_base = "https://api.openai.com/v1"
api_key = ""             # or AGENT_WINDOWS_GROUNDING_API_KEY env var
timeout = 30

[screenshot]
backend = "auto"         # auto / dxcam / mss / pillow
max_width = 1920
max_height = 1080

[tree]
max_depth = 30
```

## Requirements

- Windows 10/11
- Python 3.13+
- Dependencies: typer, comtypes, pywin32, Pillow, mss, openai

## License

Apache 2.0 (uia module vendored from [yinkaisheng/Python-UIAutomation-for-Windows](https://github.com/yinkaisheng/Python-UIAutomation-for-Windows) and [Windows-MCP](https://github.com/_ptrs/windows-mcp))
