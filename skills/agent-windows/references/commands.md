---
name: commands
description: "Complete command reference for agent-windows. Every command's syntax, parameters, defaults, output format, and examples. Consult when you need exact parameter names or output shapes."
allowed-tools: Bash(agent-windows:*)
---

# Command Reference

Complete reference for every agent-windows command. Parameters marked **required** must be provided; all others are optional with defaults shown.

For conceptual guidance (when to use which command, interaction priorities, workflows), read the main `SKILL.md`.

---

## Observation

### observe

Combined observation: snapshot + screenshot + optional visual grounding.

```bash
agent-windows observe [--snapshot] [--screenshot] [--ground DESC]
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `--snapshot` | bool | `false` | Include UIA tree snapshot |
| `--screenshot` | bool | `false` | Include screenshot |
| `--ground` | str | `""` | Visual grounding with element description |

When no flags are given, defaults to `--snapshot --screenshot`.

**Output**: Desktop State (cursor, resolution, active window), Windows table, Accessibility Tree, Interactive Elements table.

### snapshot

UIA accessibility tree traversal. Returns the full structured text view.

```bash
agent-windows snapshot
```

No parameters. Uses config `tree.max_depth` (default 15), `tree.max_elements` (default 200), `tree.timeout` (default 10s), `tree.include_invisible`, `tree.include_offscreen`.

**Output**: Same as observe but without screenshot file. Contains the Interactive Elements table with `@eN` refs.

### screenshot

Capture a screenshot, optionally cropped to a region or UIA element.

```bash
agent-windows screenshot [-o PATH] [--monitor N] [-s @eN] [--bbox X1,Y1,X2,Y2]
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `--output` / `-o` | str | `""` | Save to file path |
| `--monitor` | int | `-1` | Monitor index (-1 = all monitors) |
| `--selector` / `-s` | str | `""` | UIA element ref (e.g. `@e3`) |
| `--bbox` | str | `""` | Crop region: `x1,y1,x2,y2` |

`--selector` resolves the element's bounding box and crops to it. `--bbox` specifies pixel coordinates directly. If both given, `--selector` takes priority.

**Output**: `{output, path, width, height}`

### ground

Visual model grounding: locate element coordinates from a description.

```bash
agent-windows ground "Submit button"
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `description` | str | **required** | Element description to locate |

**Output**: `{output, x, y, confidence, method, screenshot_path}`

Precision: ±~100 px. Uses a two-pass coarse-to-fine strategy. Prefer `snapshot` for UIA-visible elements.

### ask-vision

Free-form visual Q&A: screenshot + custom prompt → raw model response.

```bash
agent-windows ask-vision "What windows are visible?" [-i IMAGE] [-m MODEL]
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `prompt` | str | **required** | Custom prompt for the vision model |
| `--image` / `-i` | str | `""` | Path to image file (default: fresh screenshot) |
| `--model` / `-m` | str | `""` | Override vision model |
| `--output` / `-o` | str | `""` | Save screenshot to path |

**Output**: `{output, response, screenshot_path, model_used}`

### detect

Detect multiple visual targets with bounding boxes.

```bash
agent-windows detect "PDF file icons" [--max-targets N] [-i IMAGE] [-m MODEL]
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `description` | str | **required** | Description of targets to detect |
| `--image` / `-i` | str | `""` | Path to image file (default: screenshot) |
| `--model` / `-m` | str | `""` | Override vision model |
| `--max-targets` | int | `20` | Maximum number of targets |
| `--output` / `-o` | str | `""` | Save annotated screenshot to path |

**Output**: `{output, description, count, targets, screenshot_path}`. Each target: `{index, label, box: {x1, y1, x2, y2}}`.

### get-text

Get text content of a UIA element by selector.

```bash
agent-windows get-text @e3
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `selector` | str | **required** | UIA element ref (e.g. `@e3`) |

**Output**: `{output, selector, text, control_type}`. `text` is the element's `name` property from UIA.

### is visible / is enabled

Check element state.

```bash
agent-windows is visible @e3
agent-windows is enabled @e3
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `selector` | str | **required** | UIA element ref (e.g. `@e3`) |

**Output**: `{output, selector, check, result}`. `result` is `true` or `false`.

- `visible` = element is not offscreen (`CurrentIsOffscreen == false`)
- `enabled` = element is enabled (`CurrentIsEnabled == true`)

### highlight

Highlight a rectangular region on a screenshot.

```bash
agent-windows highlight 100 200 500 400 [--label "Button"] [--color blue]
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `x1` | int | **required** | Left |
| `y1` | int | **required** | Top |
| `x2` | int | **required** | Right |
| `y2` | int | **required** | Bottom |
| `--label` / `-l` | str | `""` | Label text |
| `--color` / `-c` | str | `"red"` | Highlight color |
| `--image` / `-i` | str | `""` | Source image (default: fresh screenshot) |
| `--output` / `-o` | str | `""` | Save to path |

**Output**: `{output, path, regions}`

### diff

Pixel-level diff between two screenshots.

```bash
agent-windows diff baseline.png [-c current.png] [-t 30]
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `baseline` | str | **required** | Baseline image path |
| `--compare` / `-c` | str | `""` | Compare image path (default: fresh screenshot) |
| `--threshold` / `-t` | int | `30` | Pixel diff threshold (0-255) |
| `--output` / `-o` | str | `""` | Save diff image to path |

**Output**: `{output, diff_pixels, total_pixels, diff_percent, match, diff_path}`

### cleanup

Clean up old/oversized screenshots.

```bash
agent-windows cleanup
```

No parameters. Uses config `screenshot.max_screenshot_age_days` (default 7) and `screenshot.max_screenshot_dir_mb` (default 500).

**Output**: `{output, deleted_by_age, deleted_by_size, total_kept, dir_size_mb}`

---

## Input (Mouse)

### click

Mouse click at coordinates.

```bash
agent-windows click 500 300 [--right] [--double]
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `x` | int | **required** | X coordinate |
| `y` | int | **required** | Y coordinate |
| `--right` | bool | `false` | Right-click |
| `--double` | bool | `false` | Double-click |

**Output**: `{output}`

### dblclick

Double-click at coordinates.

```bash
agent-windows dblclick 500 300 [--right]
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `x` | int | **required** | X coordinate |
| `y` | int | **required** | Y coordinate |
| `--right` | bool | `false` | Right-button double-click |

**Output**: `{output}`

### hover

Move mouse and trigger hover event (tooltips, highlights).

```bash
agent-windows hover 500 300
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `x` | int | **required** | X coordinate |
| `y` | int | **required** | Y coordinate |

**Output**: `{output}`

### move

Move mouse to coordinates (no click).

```bash
agent-windows move 500 300
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `x` | int | **required** | X coordinate |
| `y` | int | **required** | Y coordinate |

**Output**: `{output}`

### mouse-down / mouse-up

Press and hold / release mouse button. Use together for custom drag sequences.

```bash
agent-windows mouse-down 500 300 [--button left|right|middle]
agent-windows mouse-up 500 300 [--button left|right|middle]
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `x` | int | **required** | X coordinate |
| `y` | int | **required** | Y coordinate |
| `--button` | str | `"left"` | `left`, `right`, or `middle` |

**Output**: `{output}`

### drag

Drag from start to end coordinates.

```bash
agent-windows drag 100 200 400 500 [--right]
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `x1` | int | **required** | Start X |
| `y1` | int | **required** | Start Y |
| `x2` | int | **required** | End X |
| `y2` | int | **required** | End Y |
| `--right` | bool | `false` | Right-button drag |

**Output**: `{output}`

### scroll

Scroll at coordinates.

```bash
agent-windows scroll 500 300 [--direction down] [--amount 3]
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `x` | int | **required** | X coordinate |
| `y` | int | **required** | Y coordinate |
| `--direction` | str | `"down"` | `up` or `down` |
| `--amount` | int | `3` | Scroll amount |

**Output**: `{output}`

---

## Input (Keyboard)

### type

Type text using Unicode direct input (bypasses IME).

```bash
agent-windows type "Hello World" [--at X,Y] [--enter] [--clear]
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `text` | str | **required** | Text to type |
| `--at` | str | `""` | Click at `x,y` before typing |
| `--enter` | bool | `false` | Press Enter after typing |
| `--clear` | bool | `false` | Select-all (Ctrl+A) and delete before typing |

**Output**: `{output}`

### shortcut

Send keyboard shortcut.

```bash
agent-windows shortcut ctrl+c
agent-windows shortcut alt+f4
agent-windows shortcut ctrl+shift+escape
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `keys` | str | **required** | Shortcut notation, e.g. `ctrl+c` |

Modifiers: `ctrl`, `alt`, `shift`, `win`. Special keys: `enter`, `escape`, `tab`, `space`, `backspace`, `delete`, `up`, `down`, `left`, `right`, `home`, `end`, `pageup`, `pagedown`, `f1`–`f12`.

**Output**: `{output}`

---

## Application Management

### app launch

Launch application by name or executable path.

```bash
agent-windows app launch Notepad
agent-windows app launch "C:\Program Files\app\test.exe" --args --flag
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `name_or_path` | str | **required** | App name or executable path |
| `args` | list[str] | `None` | Application arguments |

**Output**: Delegates to `launch_app()`.

### app switch

Switch to (bring to front) a window by title or process name.

```bash
agent-windows app switch "Notepad"
agent-windows app switch chrome
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `name_or_title` | str | **required** | Window title or process name |

**Output**: Delegates to `switch_window()`.

### app list

List all visible windows.

```bash
agent-windows app list
```

No parameters.

**Output**: Window list with title, process name, handle, position, active status.

### app resize

Resize and optionally reposition a window.

```bash
agent-windows app resize "Notepad" --width 800 --height 600
agent-windows app resize "Notepad" --width 800 --height 600 --x 100 --y 100
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `name_or_title` | str | **required** | Window title or process name |
| `--width` | int | **required** | Window width |
| `--height` | int | **required** | Window height |
| `--x` | int | `-1` | X position (-1 = keep current) |
| `--y` | int | `-1` | Y position (-1 = keep current) |

**Output**: Delegates to `resize_window()`.

---

## System

### shell

Run a PowerShell command.

```bash
agent-windows shell "Get-Process | Select-Object -First 5"
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `command` | str | **required** | PowerShell command |

**Output**: Command stdout/stderr.

### fs read / fs write / fs list

File system operations.

```bash
agent-windows fs read C:\Users\test\file.txt
agent-windows fs write C:\Users\test\out.txt "Hello, World!"
agent-windows fs list C:\Users
```

| Command | Parameters |
|---------|-----------|
| `fs read` | `path` (str, **required**) |
| `fs write` | `path` (str, **required**), `content` (str, **required**) |
| `fs list` | `path` (str, **required**) |

### clipboard get / clipboard set

Clipboard operations.

```bash
agent-windows clipboard get
agent-windows clipboard get --keyboard          # Simulate Ctrl+C first
agent-windows clipboard set "copied text"
agent-windows clipboard set "text" --keyboard   # Simulate Ctrl+V after
```

| Command | Parameters |
|---------|-----------|
| `clipboard get` | `--keyboard` / `-k` (bool, optional) |
| `clipboard set` | `text` (str, **required**), `--keyboard` / `-k` (bool, optional) |

Without `--keyboard`, uses the system clipboard API (PowerShell). With `--keyboard`, simulates Ctrl+C/V keystrokes. Config default: `clipboard.method` = `"system"`.

### process list / process kill

Process management.

```bash
agent-windows process list
agent-windows process kill 1234       # By PID
agent-windows process kill notepad     # By name
```

| Command | Parameters |
|---------|-----------|
| `process list` | (none) |
| `process kill` | `pid_or_name` (str, **required**) |

---

## Control

### wait

Wait for a duration, an element, or text to appear.

```bash
agent-windows wait 3                               # Sleep 3 seconds
agent-windows wait @e5 --timeout 10                 # Wait for element
agent-windows wait --text "完成" --timeout 15        # Wait for text
agent-windows wait @e5 --interval 0.2 --timeout 5   # Custom poll interval
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `target` | str | `""` | Seconds to wait, or element selector (e.g. `@e3`) |
| `--timeout` / `-t` | float | `30` | Max seconds to wait (element/text mode) |
| `--text` | str | `""` | Wait until element name contains this text |
| `--interval` | float | `0` | Poll interval (0 = use config default 0.5s) |

Three modes:
- **Sleep**: `wait 3` — sleeps for N seconds.
- **Element**: `wait @e3 --timeout 10` — polls `resolve_element()` until found or timeout.
- **Text**: `wait --text "Done" --timeout 10` — polls `traverse_tree()` for matching element name.

Raises error code `-1003` (WAIT_TIMEOUT) on timeout.

### batch

Execute multiple commands in one IPC round-trip.

```bash
agent-windows batch '[{"method":"click","params":{"x":500,"y":300}}]'
agent-windows batch --file commands.json
echo '[{"method":"snapshot"}]' | agent-windows batch -
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `commands_json` | str | `""` | JSON array of commands, or `-` for stdin |
| `--file` / `-f` | str | `""` | Read commands from JSON file |
| `--stop-on-error` | bool | `true` | Stop on first error |
| `--continue-on-error` | bool | `false` | Continue after errors |
| `--delay` / `-d` | float | `0.0` | Delay in seconds between commands |

Each command: `{"method": "<name>", "params": {<optional>}}`. Nested batch is forbidden.

**Output**: `{output, results, total, success}`. Each result: `{method, ok, result?, error?}`.

---

## Daemon & Config

### daemon start / stop / status

Manage the daemon process. These run locally (no IPC dispatch).

```bash
agent-windows daemon start     # Launch daemon subprocess
agent-windows daemon stop      # Send shutdown IPC call
agent-windows daemon status    # Check if running, PID, version
```

### config init / show / set

Configuration management. These run locally.

```bash
agent-windows config init              # Create default config file
agent-windows config show              # Print full config
agent-windows config set tree.max_depth 50   # Set a config value
```

Config file: `~/.agent-windows/config.toml`

---

## Global Options

Available on all commands:

| Flag | Description |
|------|-------------|
| `--no-daemon` | Direct execution (bypass daemon, for debugging) |
| `--format text\|json` | Output format |
| `--debug` | Verbose logging |
