<!-- Reference doc — loaded on demand via the table in SKILL.md, not discovered as a standalone skill. -->

# Command Reference

Complete reference for every agent-windows command. Parameters marked **required** must be provided; all others are optional with defaults shown.

For conceptual guidance (when to use which command, interaction priorities, workflows), read the main `SKILL.md`.

---

## Observation

### observe

Combined observation: snapshot + screenshot + optional visual grounding.

```bash
agent-windows observe [--snapshot] [--screenshot] [--ground DESC]
agent-windows observe -d 0                  # scope everything to display 0
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `--snapshot` | bool | `false` | Include UIA tree snapshot |
| `--screenshot` | bool | `false` | Include screenshot |
| `--ground` | str | `""` | Visual grounding with element description |
| `--display` / `-d` | list[int] | `[]` | Limit snapshot data to the given display index(es), repeatable; the screenshot is cropped to the selected displays' union. Omit for the full desktop. |

When no flags are given, defaults to `--snapshot --screenshot`.

`--display` scopes the snapshot (windows/elements/cursor, see `snapshot`) and crops the screenshot to the union of the selected display(s).

**Output**: Desktop State (cursor, resolution, active window), Windows table, Accessibility Tree, Interactive Elements table.

### snapshot

UIA accessibility tree traversal. Returns the full structured text view.

```bash
agent-windows snapshot
agent-windows snapshot -d 0            # only the primary display
agent-windows snapshot -d 0 -d 1       # union of displays 0 and 1
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `--display` / `-d` | list[int] | `[]` | Limit windows/interactive-elements/cursor to the given display index(es), repeatable (e.g. `-d 0 -d 1`). Omit for the full desktop. |

Uses config `tree.max_depth` (default 15), `tree.max_elements` (default 200), `tree.timeout` (default 10s), `tree.include_invisible`, `tree.include_offscreen`.

**Display filtering**: `-d` clips each window/element bounding box to the selected display(s)' union and drops items that don't overlap (intersect-clip, not "window belongs to one display" — a window spanning two displays is kept, clipped to the selected one). `@eN` refs are renumbered against the filtered list. The cursor is reported as `N/A` when it's off the selected display(s). The Accessibility Tree text block is NOT filtered (it's free text without per-line coordinates); a note is shown when a filter is active.

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
agent-windows ground "Submit button" --single-pass   # 1 API call, ~2x faster, lower precision
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `description` | str | **required** | Element description to locate |
| `--single-pass` | bool | `false` | Skip the fine-pass API call: one round-trip instead of two (faster, lower cost) at the cost of precision — coarse coordinate only. Overrides config `vlm.single_pass` for this call. |

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

- `visible` = element is on-screen
- `enabled` = element is enabled

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

Most mouse commands accept either explicit `X Y` coordinates or a `--selector`/`-s` `@eN` element ref from a `snapshot`. `--selector` overrides `X Y` when both are given; the element's bbox center is the click point.

### click

Mouse click at coordinates or an element.

```bash
agent-windows click 500 300 [--right] [--double]
agent-windows click --selector @e3 [--right]      # click element center
agent-windows click -s @e3                          # short form
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `X Y` | int | **required*** | X and Y coordinates |
| `--selector` / `-s` | str | `""` | Element ref (`@eN`); overrides `X Y` |
| `--right` | bool | `false` | Right-click |
| `--double` | bool | `false` | Double-click |

\* `X Y` is required unless `--selector` is given.

**Output**: `{output}`

### dblclick

Double-click at coordinates or an element.

```bash
agent-windows dblclick 500 300 [--right]
agent-windows dblclick -s @e3
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `X Y` | int | **required*** | X and Y coordinates |
| `--selector` / `-s` | str | `""` | Element ref (`@eN`); overrides `X Y` |
| `--right` | bool | `false` | Right-button double-click |

\* `X Y` is required unless `--selector` is given.

**Output**: `{output}`

### hover

Move mouse and trigger hover event (tooltips, highlights).

```bash
agent-windows hover 500 300
agent-windows hover -s @e3
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `X Y` | int | **required*** | X and Y coordinates |
| `--selector` / `-s` | str | `""` | Element ref (`@eN`); overrides `X Y` |

\* `X Y` is required unless `--selector` is given.

**Output**: `{output}`

### move

Move mouse to coordinates or an element (no click).

```bash
agent-windows move 500 300
agent-windows move -s @e3
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `X Y` | int | **required*** | X and Y coordinates |
| `--selector` / `-s` | str | `""` | Element ref (`@eN`); overrides `X Y` |

\* `X Y` is required unless `--selector` is given.

**Output**: `{output}`

### mouse-down / mouse-up

Press and hold / release mouse button. Use together for custom drag sequences.

```bash
agent-windows mouse-down 500 300 [--button left|right|middle]
agent-windows mouse-down -s @e3 [--button left|right|middle]
agent-windows mouse-up -s @e3 [--button left|right|middle]
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `X Y` | int | **required*** | X and Y coordinates |
| `--selector` / `-s` | str | `""` | Element ref (`@eN`); overrides `X Y` |
| `--button` | str | `"left"` | `left`, `right`, or `middle` |

\* `X Y` is required unless `--selector` is given.

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

`drag` takes explicit coordinates only (two endpoints); resolve element centers via `snapshot` first if needed.

**Output**: `{output}`

### scroll

Scroll at coordinates or an element.

```bash
agent-windows scroll 500 300 [--direction down] [--amount 3]
agent-windows scroll -s @e3 [--direction up]
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `X Y` | int | **required*** | X and Y coordinates |
| `--selector` / `-s` | str | `""` | Element ref (`@eN`); overrides `X Y` |
| `--direction` | str | `"down"` | `up` or `down` |
| `--amount` | int | `3` | Scroll amount |

\* `X Y` is required unless `--selector` is given.

**Output**: `{output}`

---

## Input (Keyboard)

### type

Type text using Unicode direct input (bypasses IME).

```bash
agent-windows type "Hello World" [--at X,Y] [--at-selector @eN] [--enter] [--clear] [--caret idle]
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `text` | str | **required** | Text to type |
| `--at` | str | `""` | Click at `x,y` before typing |
| `--at-selector` / `--ats` | str | `""` | Click at element `@eN` before typing; overrides `--at` |
| `--enter` | bool | `false` | Press Enter after typing |
| `--clear` | bool | `false` | Select-all (Ctrl+A) and delete before typing |
| `--caret` | str | `"idle"` | Move caret before typing: `start` (Home), `idle` (leave as-is), or `end` (End) |

`--at-selector` resolves the element's bbox center and clicks there before typing; it overrides `--at` when both are given. `--caret` moves the text caret after the click (or in the already-focused field when `--at`/`--at-selector` are omitted): `start` presses Home, `end` presses End, `idle` leaves it where the platform placed it. Applied before `--clear`, so `--caret start --clear` selects-from-start semantics still wipe the field. Omitting the flag (or sending `null`) uses `idle`.

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

## Batch Interaction

### multi-select

Click multiple targets in one call, optionally holding Ctrl for multi-selection.

```bash
# Coordinates: [[x,y], ...]
agent-windows multi-select '[[100,200],[300,400]]'
agent-windows multi-select '[[100,200]]' --no-ctrl         # independent clicks

# Element refs: ["@e1", "@e3", ...] (resolved to bbox centers)
agent-windows multi-select '["@e1","@e3"]' --selectors

# From a file or stdin
agent-windows multi-select --file targets.json --selectors
echo '[[1,2],[3,4]]' | agent-windows multi-select -
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `JSON` | str | **required*** | JSON array of `[x,y]` pairs or `@eN` strings, or `-` for stdin |
| `--file` / `-f` | str | `""` | Read JSON from file (overrides argument) |
| `--selectors` | bool | `false` | Treat JSON values as `@eN` element refs (resolved to centers) |
| `--press-ctrl` / `--no-ctrl` | bool | `true` | Hold Ctrl across all clicks (multi-select) vs independent clicks |

\* `JSON` is required unless `--file` or stdin (`-`) is given.

With `--press-ctrl` (default), Ctrl is held down across every click so each toggles selection (file multi-select, checkbox groups) rather than activating. With `--no-ctrl`, each click is independent. Capped at 50 targets.

**Output**: `{output, count, press_ctrl, locations}`

### multi-edit

Type text into multiple fields in one call.

```bash
# Coordinates: [[x,y,text], ...]
agent-windows multi-edit '[[100,200,"Alice"],[300,400,"admin"]]'

# Element refs: [["@eN", text], ...]
agent-windows multi-edit '[["@e1","Alice"],["@e3","admin"]]' --selectors

agent-windows multi-edit --file form.json --selectors
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `JSON` | str | **required*** | JSON array of `[x,y,text]` triples or `[@eN,text]` pairs, or `-` for stdin |
| `--file` / `-f` | str | `""` | Read JSON from file (overrides argument) |
| `--selectors` | bool | `false` | Treat JSON pairs as `[@eN, text]` (resolved to centers) |

\* `JSON` is required unless `--file` or stdin (`-`) is given.

Each field is clicked, cleared (Ctrl+A + Delete), then typed into. Capped at 50 targets; each text capped at 10000 chars. Fields are committed sequentially — if field N fails, fields 1..N-1 have already been typed and the error propagates; verify with `snapshot` afterward, or issue separate `type` calls when rollback matters.

**Output**: `{output, count, fields}`

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

**Output**: PID of the launched process.

### app switch

Switch to (bring to front) a window by title or process name.

```bash
agent-windows app switch "Notepad"
agent-windows app switch chrome
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `name_or_title` | str | **required** | Window title or process name |

**Output**: Window handle of the switched-to window.

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

**Output**: New window position and size.

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

### wait-for

Wait until a multi-condition UI predicate is satisfied. Polls the accessibility tree until the condition matches or the timeout elapses.

```bash
agent-windows wait-for text_exists --text "完成"              # any visible text
agent-windows wait-for active_window --window "Notepad"       # active window title
agent-windows wait-for element_exists --text OK --timeout 10  # an element exists
agent-windows wait-for element_enabled --text Submit           # ...and is enabled
agent-windows wait-for focused_element --text "Search"         # ...and has focus
agent-windows wait-for enabled --text OK --interval 0.2        # short alias + custom poll
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `condition` | str | **required** | One of: `text_exists`, `active_window`, `element_exists`, `element_enabled`, `focused_element` (short aliases `text`/`window`/`element`/`enabled`/`focused` also accepted) |
| `--text` | str | `""` | Case-sensitive substring. Matched against element name/control_type/metadata values, or window titles depending on the condition |
| `--window` | str | `""` | Case-sensitive substring to scope element matching to a window title |
| `--timeout` / `-t` | float | `30` | Max seconds to wait (capped at 300s) |
| `--interval` | float | `0` | Poll interval (0 = use config default 0.5s) |

Conditions:

| Condition | Matches when |
|-----------|--------------|
| `text_exists` | Any visible text — active window title, any window title, or any interactive element's name/control_type/window_name/metadata — contains `--text` |
| `active_window` | The active window's title contains `--text` (or `--window`) |
| `element_exists` | An interactive element matches `--text` and (if given) `--window` |
| `element_enabled` | As `element_exists`, and the element is enabled |
| `focused_element` | As `element_exists`, and the element has keyboard focus |

**Matching is case-sensitive substring** (e.g. `--text OK` matches `"OK Button"` but not `"ok button"`). This differs from some other tools' case-insensitive matching — match the exact case shown in `snapshot` output, or use a substring that is case-stable (e.g. a control type like `Button`).

Like `wait`, the CLI polls the daemon with short non-blocking probes so the daemon's worker is never blocked for the wait duration. Raises error code `-1003` (WAIT_TIMEOUT) on timeout, `-32602` (INVALID_PARAMS) for an unknown/invalid condition.

### batch

Execute multiple commands in one call.

```bash
agent-windows batch '[{"method":"click","params":{"x":500,"y":300}}]'
agent-windows batch --file commands.json
echo '[{"method":"snapshot"}]' | agent-windows batch -
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `commands_json` | str | `""` | JSON array of commands, or `-` for stdin |
| `--file` / `-f` | str | `""` | Read commands from JSON file |
| `--delay` / `-d` | float | `0.0` | Delay in seconds between commands |

Error handling is a single boolean toggle: `--stop-on-error` (default) or `--continue-on-error`. These are a Typer boolean pair (`stop_on_error`), not two independent flags — pass one or the other:

```bash
agent-windows batch --file cmds.json --continue-on-error
```

Each command: `{"method": "<name>", "params": {<optional>}}`. Nested batch is forbidden.

**Output**: `{output, results, total, success}`. Each result: `{method, ok, result?, error?}`.

---

## Daemon & Config

### daemon start / stop / status

Manage the daemon process.

```bash
agent-windows daemon start     # Start the daemon
agent-windows daemon stop      # Stop the daemon
agent-windows daemon status    # Check if running, PID, version
```

### config init / show / set / doctor

Configuration management. The launcher forwards these to the bundled Python CLI transparently, so use `agent-windows config ...` directly.

```bash
agent-windows config init              # Create default config file
agent-windows config show              # Print full config
agent-windows config set tree.max_depth 50   # Set a config value
agent-windows config doctor            # Check whether the vision model is configured
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
