# agent-windows

Windows Computer-Use CLI for AI Agents. Provides desktop screenshot, UIA accessibility tree traversal, visual coordinate grounding, and mouse/keyboard automation through a fast Client-Daemon architecture.

## Highlights

- **Bring-your-own vision model.** Configure any cheap multimodal model (GPT-4o-mini, Claude, Gemini, etc.) and the tool uses it to locate elements that aren't in the accessibility tree — icon-only buttons, canvas, custom-drawn UI.
- **Works without a multimodal agent.** Even if your driving LLM has no vision capability, `ground`/`detect` return ready-to-click coordinates by offloading the image work to a dedicated small model — saving context tokens and cost.
- **Pixel-precise by default.** The UIA accessibility tree gives exact coordinates for standard controls; vision is the fallback for the rest.

## Install

Requires [uv](https://docs.astral.sh/uv/) (the Python package runner used below). Install it first if you don't have it:

```bash
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

Then install agent-windows (recommended — adds it to your PATH as a persistent command):

```bash
uv tool install agent-windows
```

This installs the **native Rust launcher** (`agent-windows.exe`, ~12ms cold start) plus the Python CLI as `agent-windows-python` (fallback for `--no-daemon`; the launcher also delegates `config` to it).

One-off trial (runs without installing):

```bash
uvx agent-windows observe
```

> **`uv tool install` is recommended over `uvx` for regular use.** The daemon is long-lived and auto-restarts when its version no longer matches the CLI's; `uv tool install` pins both versions together, while `uvx` runs from a cache that can be silently refreshed (new release, cache expiry, `uvx -U`), triggering unwanted daemon restarts. See [Troubleshooting](#) for details.

From source (a checkout of this repo):

```bash
uv sync
uv run agent-windows ...
```

To rebuild the native launcher from source (requires Rust + MSVC + Windows SDK):

```bash
cd src/launcher && build.cmd build --release
# the prebuilt exe at src/launcher/prebuilt/ is what ships in the wheel
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
# Single-pass: one API call, ~2x faster, lower precision (overrides config vlm.single_pass)
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
- **Serial execution**: The daemon runs one command at a time. This is required because UIA/COM is apartment-bound and `SendInput` mutates global GUI state — concurrent commands would race. A single client is naturally serial; the only case it matters is multiple agents sharing one daemon (they serialize against each other). For isolation, run separate daemons (one user account per agent — the pipe path is per-user).

## Commands

### Observation
| Command | Description |
|---------|-------------|
| `observe [--snapshot] [--screenshot] [--ground DESC]` | Combined observation |
| `snapshot` | UIA accessibility tree |
| `screenshot [-o PATH] [-s @eN] [--bbox X1,Y1,X2,Y2] [--annotate] [--grid-w N] [--grid-h N]` | Screen capture; `--annotate` overlays numbered `@eN` boxes (full-screen) + optional `%` reference grid |
| `ground DESCRIPTION [--single-pass]` | Visual coordinate grounding (`--single-pass`: 1 API call, ~2x faster, lower precision) |
| `detect DESCRIPTION [-i IMAGE] [--max-targets N]` | Multi-target bounding box detection |
| `ask-vision PROMPT [-i IMAGE] [-m MODEL]` | Free-form visual Q&A |
| `highlight X1 Y1 X2 Y2 [-l LABEL] [-c COLOR]` | Highlight region on screenshot |
| `diff BASELINE [-c COMPARE] [-t THRESHOLD]` | Pixel-level screenshot diff |
| `get-text SELECTOR` | Get text content of a UIA element |
| `is visible/enabled SELECTOR` | Check element state |

### Input
| `click X Y \| -s @eN [--right] [--double]` | Mouse click |
| `dblclick X Y \| -s @eN [--right]` | Double-click |
| `hover X Y \| -s @eN` | Hover (trigger tooltips/highlights) |
| `mouse-down X Y \| -s @eN [--button left\|right\|middle]` | Press and hold mouse button |
| `mouse-up X Y \| -s @eN [--button left\|right\|middle]` | Release mouse button |
| `type TEXT [--at X,Y \| --ats @eN] [--enter] [--clear] [--caret start\|idle\|end]` | Type text (Unicode direct input) |
| `scroll X Y \| -s @eN [--direction up\|down] [--amount N]` | Scroll |
| `move X Y \| -s @eN` | Move mouse |
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
| `notify TITLE MESSAGE [--app-id ID]` | Windows toast notification (defaults to PowerShell's AUMID) |
| `registry get/set/delete/list PATH [NAME] [VALUE] [--type T]` | Windows Registry operations (typed values: String/DWord/QWord/MultiString/Binary) |
| `cleanup` | Clean up old/oversized screenshots |
| `wait SECONDS|SELECTOR [--text TEXT] [--timeout N] [--interval N]` | Wait for duration, element, or text |
| `batch JSON/--file PATH [--delay N] [--continue-on-error]` | Execute multiple commands in one IPC round-trip |
| `multi-select JSON/--file PATH [--selectors] [--no-ctrl]` | Click multiple targets (Ctrl held by default) |
| `multi-edit JSON/--file PATH [--selectors]` | Type into multiple fields in one call |

### Daemon & Config
| Command | Description |
|---------|-------------|
| `daemon start/stop/status` | Daemon management |
| `config init/show/set/doctor` | Configuration (delegated to the Python CLI) |

### Global Options
| Flag | Description |
|------|-------------|
| `--no-daemon` | Direct execution (bypass daemon, for debugging) |
| `--format text\|json` | Output format |
| `--debug` | Verbose logging |

## Configure a Vision Model (optional, recommended)

`ground`, `detect`, and `ask-vision` need a multimodal model to read the screen. Configure one with `config set`:

```bash
# 1. API key (or export AGENT_WINDOWS_VLM_API_KEY instead)
agent-windows config set vlm.api_key sk-xxxxxxxx

# 2. Engine: openai (default) / anthropic / gemini
agent-windows config set vlm.engine openai

# 3. Model (a cheap multimodal model is enough)
agent-windows config set vlm.model gpt-4o-mini

# 4. (Non-OpenAI endpoints only) API base
# agent-windows config set vlm.api_base https://api.openai.com/v1

# 5. Verify
agent-windows config doctor
```

> The `config` command is handled by the launcher: it forwards transparently
> to the bundled Python CLI (`agent-windows-python`), which owns the config
> schema and DPAPI encryption. No separate command name needed.

> **Why configure one even if your agent model is multimodal?** Screenshots consume a lot of context tokens. Routing the image work to a dedicated cheap model via `ground`/`detect` returns ready-to-click coordinates in one short call, cutting both context usage and cost. `ground`'s two-pass coarse-to-fine strategy also lands clicks more precisely than a single whole-screenshot guess, and `detect` returns structured bounding boxes for every match.

## Configuration

Config file: `~/.agent-windows/config.toml` (auto-created with owner-only permissions)

> **Security**: The `vlm.api_key` is **encrypted at rest** with Windows DPAPI (current-user scope); the config file is also restricted to your Windows account via an owner-only ACL. Always set the key through `config set vlm.api_key sk-...` (or the `AGENT_WINDOWS_VLM_API_KEY` env var) — it is encrypted transparently on save and decrypted transparently on load, so you never handle the encrypted form. API keys in `config show` output are automatically masked. For CI/CD or shared environments, prefer the `AGENT_WINDOWS_VLM_API_KEY` environment variable — it takes priority over the config file and is never written to disk.

> **When changes take effect**: The daemon re-reads the config file on every command (checking the file's modification time), so `config set` applies to the **next command** — no daemon restart needed. The one exception is `daemon.idle_timeout_minutes`: it's read once at daemon startup, so changing it requires a restart (`daemon stop` then `daemon start`, or just let the old one idle out).

```toml
[vlm]
enabled = true
engine = "openai"       # openai / anthropic / gemini
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
```

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

- Windows 8.1 / 10 / 11
- Python 3.13+
- Dependencies: typer, comtypes, pywin32 (≥308), Pillow, mss, openai

> **pywin32 note**: modern pywin32 wheels (≥308) self-configure their DLLs via a `.pth` bootstrap at install time, so `uv tool install` / `uvx` works out of the box. If you ever hit `pythoncom3xx.dll` / `pywintypes3xx.dll` not found on an older pywin32 or a manually-managed environment, run `python Scripts\pywin32_postinstall.py -install` from your install prefix.

## License

MIT (the vendored `uia` module is from [yinkaisheng/Python-UIAutomation-for-Windows](https://github.com/yinkaisheng/Python-UIAutomation-for-Windows), Apache 2.0; some patterns adapted from [Windows-MCP](https://github.com/CursorTouch/Windows-MCP), MIT).
