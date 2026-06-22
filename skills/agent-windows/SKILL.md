---
name: agent-windows
description: "Windows desktop automation CLI for AI agents. Use when the user needs to interact with the Windows desktop: open apps, read the screen, click / type / press shortcut keys / scroll / drag, execute shell commands, manage files, processes, and clipboard, or automate any Windows GUI task. Triggers include requests to open an application, click a button, type text, take a screenshot, read screen content, manage windows, run PowerShell, or automate any desktop workflow on Windows."
allowed-tools: Bash(agent-windows:*)
---

# agent-windows

Windows desktop automation CLI. Open apps, read the screen, click, type, press shortcuts, scroll, drag, run shell, manage files/processes/clipboard — automate any Windows GUI task.

## Install

Check whether it's already installed:

```bash
agent-windows --version          # prints version if installed
```

If not, install (recommended — pins `agent-windows` to your PATH):

```bash
uv tool install agent-windows
```

(`uv` not installed? Run: `powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"`)

One-off trial (runs without installing): `uvx agent-windows observe`.

> Prefer `uv tool install` over `uvx` for regular use — it pins the version, while `uvx` can silently bump it and trigger daemon restarts.

## The Core Loop

Every desktop interaction follows this cycle:

```
1. agent-windows observe              # See the screen (snapshot + screenshot)
2. Find target in Interactive Elements table
3. agent-windows click -s @eN         # Act via selector (preferred)
   # or: extract coords from Position, compute center, click cx cy
4. agent-windows observe              # Verify the result
```

**Critical rule**: `@eN` refs from the Interactive Elements table go **stale** after any UI change (click, type, shortcut). Always re-observe before the next interaction.

## Quickstart

```bash
# Initialize config (first time only)
agent-windows config init

# See the screen
agent-windows observe

# Click a button (find @e3 in snapshot)
agent-windows click -s @e3            # via selector (preferred)
agent-windows click 450 320           # or explicit coordinates

# Type text
agent-windows type "Hello World" --enter

# Keyboard shortcut
agent-windows shortcut ctrl+s
```

## Reading the Screen

Three observation tools, each suited to different needs:

### observe — Combined (default: snapshot + screenshot)

```bash
agent-windows observe                    # snapshot + screenshot
agent-windows observe --snapshot         # snapshot only
agent-windows observe --screenshot       # screenshot only
agent-windows observe --ground "Submit"  # snapshot + screenshot + visual grounding
```

Output includes: Desktop State (cursor, resolution), Windows table, Accessibility Tree, Interactive Elements table.

### snapshot — UIA Tree Only

```bash
agent-windows snapshot
```

Returns a structured text view of the UIA accessibility tree. Contains the **Interactive Elements table** with `@eN` refs and their bounding box coordinates. This is the primary way to find clickable targets.

### screenshot — Screen Capture

```bash
agent-windows screenshot                 # Full screen
agent-windows screenshot -s @e3          # Crop to UIA element
agent-windows screenshot --bbox 100,200,500,400  # Crop to region
agent-windows screenshot -o shot.png     # Save to file
agent-windows screenshot --annotate      # Overlay numbered @eN boxes on full screen
agent-windows screenshot --annotate --grid-w 4 --grid-h 4  # Annotated + % reference grid
```

> `--annotate` re-captures the **full screen** (all displays) and draws a numbered box per interactive element from a fresh snapshot, so the `@eN` labels line up with the current `snapshot`/`observe` table. `--bbox`/`-s`/`--monitor` are ignored (warned) when annotating. Use `--grid-w N`/`--grid-h N` to draw `N` evenly-spaced reference lines with `X%`/`Y%` labels to sanity-check coordinates.

### Decision Matrix

| Tool | Precision | Best for |
|------|-----------|----------|
| `snapshot` | Pixel-exact | Named controls, dialogs, menus, standard UI |
| `ground` / `detect` | ±~100 px | Custom-drawn UI that UIA can't capture |
| `ask-vision` | Descriptive only | Understanding icons, reading screen state |

**Rule**: Prefer `snapshot` for element location. Use vision tools only when UIA can't see an element.

## Vision Model Setup

`ground`, `detect`, and `ask-vision` need a configured multimodal model to read the screen. Check if one is ready:

```bash
agent-windows config doctor      # shows whether the vision model is configured
```

If it reports **NOT READY**, help the user configure it before using vision commands:

```bash
agent-windows config set vlm.api_key sk-...      # their API key
agent-windows config set vlm.engine openai       # openai / anthropic / gemini
agent-windows config set vlm.model gpt-4o-mini   # a cheap multimodal model
agent-windows config doctor                            # verify
```

> Even when your own model is multimodal, prefer routing image work through these commands: a screenshot in context costs many tokens, while `ground`/`detect` return ready-to-click coordinates in one short call.

## The @eN Selector System

### How It Works

`@eN` references an element in the Interactive Elements table from the most recent `snapshot` or `observe` call. The table looks like:

```
## Interactive Elements
| Ref | Type    | Name     | Position           | State   |
|-----|---------|----------|--------------------|---------|
| @e1 | Button  | OK       | (400,300,500,340)  |         |
| @e2 | Edit    | Search   | (100,100,300,130)  | focused |
| @e3 | Button  | Cancel   | (520,300,620,340)  |         |
```

The State column also surfaces richer attributes when the element exposes them (TODO #15): `toggle_state:on|off|indeterminate`, `value:...`, `expand_collapse_state:expanded|collapsed|partially expanded|leaf`, `min`/`max` (sliders), `is_selected:true|false` (list/radio/tab items), `is_password:true`, `shortcut:Ctrl+S`, `help_text:...`. Use these to disambiguate similar-looking elements and read current state (e.g. a checkbox's on/off) without an extra click.

### Commands That Accept @eN Directly

These commands handle coordinate extraction internally:

```bash
agent-windows get-text @e3               # Returns element's name/text
agent-windows is visible @e3             # Check if element is on-screen
agent-windows is enabled @e3             # Check if element is enabled
agent-windows wait @e3 --timeout 10      # Wait for element to appear
agent-windows screenshot -s @e3          # Screenshot cropped to element
agent-windows click -s @e3               # Click element's bbox center
agent-windows type "text" --at-selector @e3   # Click element center, then type
agent-windows scroll -s @e3 --direction up
```

`click`, `dblclick`, `hover`, `move`, `mouse-down`, `mouse-up`, `scroll`, and `type` (`--at-selector`) all accept `--selector`/`-s` and resolve the element's bbox center for you — no manual coordinate math.

### When You Need Explicit Coordinates

For `drag` (two endpoints) or when you want a non-center click point, extract coordinates manually:

```bash
# Extract center from Position: (400,300,500,340) → center=(450,320)
agent-windows click 450 320
agent-windows type "text" --at 450,320
agent-windows drag 100 200 400 500
```

### Ref Lifecycle

- Refs are **invalidated** on any UI change (navigation, click, dialog popup, dynamic content).
- **Always re-snapshot** (or re-observe) before the next interaction.
- Never reuse refs from a previous turn — the UI may have changed completely.

For a deep dive on selectors, coordinates, and the snapshot format, read `references/selectors.md`.

## Interacting

### Mouse

```bash
agent-windows click 500 300                # Left click
agent-windows click 500 300 --right        # Right click
agent-windows click 500 300 --double       # Double click
agent-windows dblclick 500 300             # Double click (explicit)
agent-windows hover 500 300                # Hover (trigger tooltips)
agent-windows move 500 300                 # Move mouse only
agent-windows scroll 500 300 --direction down --amount 5   # Scroll
agent-windows drag 100 200 400 500         # Drag from A to B
agent-windows mouse-down 500 300           # Press and hold
agent-windows mouse-up 500 300             # Release
```

### Keyboard

```bash
agent-windows type "Hello World"                     # Type text (Unicode, bypasses IME)
agent-windows type "search term" --at 300,200        # Click field first, then type
agent-windows type "text" --enter                    # Press Enter after typing
agent-windows type "new text" --clear                # Select-all + delete first
agent-windows shortcut ctrl+c                        # Copy
agent-windows shortcut ctrl+v                        # Paste
agent-windows shortcut alt+f4                        # Close window
agent-windows shortcut win+r                         # Run dialog
```

Key notation for `shortcut`: `ctrl`, `alt`, `shift`, `win` as modifiers, joined with `+`. Special keys: `enter`, `escape`, `tab`, `space`, `backspace`, `delete`, `up/down/left/right`, `home`, `end`, `pageup`, `pagedown`, `f1`–`f12`.

### Batch Interaction

Click multiple targets or fill multiple fields in one call (caps at 50 targets; each text field capped at 10000 chars). Pass coordinates (`[[x,y],...]` / `[[x,y,text],...]`) or element refs with `--selectors` (`["@e1",...]` / `[["@e1","text"],...]`).

```bash
# Select multiple files (Ctrl held across clicks — default)
agent-windows multi-select '[[100,200],[300,400],[500,600]]'
agent-windows multi-select '["@e1","@e3","@e5"]' --selectors

# Fill a form in one shot — each field is clicked, cleared, and typed
agent-windows multi-edit '[[100,200,"Alice"],[300,400,"admin@example.com"]]'
agent-windows multi-edit '[["@e1","Alice"],["@e3","admin"]]' --selectors

# Read targets from a file or stdin
agent-windows multi-select --file targets.json --selectors
echo '[[1,2],[3,4]]' | agent-windows multi-select -
```

Use `--no-ctrl` on `multi-select` for independent clicks (no selection semantics).

## Safety Rules

### 1. Always switch window before input

Use `app switch` before any keyboard or mouse operation. Otherwise input may be sent to the wrong window — with unpredictable results.

```bash
agent-windows app switch "Notepad"
# now safe to click / type / shortcut
```

### 2. Prefer UIA tree over vision for element location

`snapshot` returns precise bounding rectangles for named UI elements. `ground` / `detect` use a vision model whose coordinates can be off by ~100 px. Use vision tools as a supplement when UIA can't see an element.

### 3. Observe before act

Always call `observe` or `snapshot` before taking action. Never click or type based on assumptions about the screen state.

## Application Management

```bash
agent-windows app list                        # List visible windows
agent-windows app launch Notepad              # Launch by name
agent-windows app launch "C:\app\test.exe"    # Launch by path
agent-windows app switch "Notepad"            # Bring window to front
agent-windows app resize "Notepad" --width 800 --height 600   # Resize window
agent-windows app resize "Notepad" --width 800 --height 600 --x 100 --y 100
```

**Canonical app workflow**: `app launch` → `observe` → interact → `observe` to verify.

## System Operations

```bash
# Shell
agent-windows shell "Get-Process | Select-Object -First 5"

# Filesystem
agent-windows fs list C:\Users
agent-windows fs read C:\Users\test\file.txt
agent-windows fs write C:\Users\test\out.txt "Hello"

# Clipboard
agent-windows clipboard get                  # Read clipboard
agent-windows clipboard set "copied text"    # Write to clipboard
agent-windows clipboard get --keyboard       # Simulate Ctrl+C first
agent-windows clipboard set "text" --keyboard # Simulate Ctrl+V after

# Process
agent-windows process list
agent-windows process kill 1234              # By PID
agent-windows process kill notepad           # By name
```

## Waiting and Verification

### wait — Three Modes

```bash
# Sleep for N seconds
agent-windows wait 3

# Wait for element to appear
agent-windows wait @e5 --timeout 10

# Wait for text to appear in any element's name
agent-windows wait --text "完成" --timeout 15

# Custom polling interval (default: 0.5s from config)
agent-windows wait @e5 --interval 0.2 --timeout 10
```

On timeout, returns error code `-1003` (WAIT_TIMEOUT).

### Element State

```bash
agent-windows is visible @e3     # Returns true if element is on-screen
agent-windows is enabled @e3     # Returns true if element is enabled
```

### Text Extraction

```bash
agent-windows get-text @e3       # Returns the element's name/text content
```

### Visual Verification

```bash
agent-windows highlight 100 200 500 400 --label "Region"    # Highlight area on screenshot
agent-windows highlight 100 200 500 400 --color blue        # Custom color
agent-windows diff baseline.png -c current.png -t 30        # Pixel diff between images
agent-windows diff baseline.png                             # Diff vs fresh screenshot
```

## Batch Execution

Execute multiple commands in one call:

```bash
# Inline JSON
agent-windows batch '[{"method":"click","params":{"x":500,"y":300}},{"method":"type","params":{"text":"hello"}}]'

# From file
agent-windows batch --file commands.json

# From stdin
echo '[{"method":"snapshot"}]' | agent-windows batch -

# Options
agent-windows batch --file cmds.json --delay 0.5 --continue-on-error
```

Each command in the array: `{"method": "<name>", "params": {<optional>}}`.

## Common Workflows

### Launch an app and type text

```bash
agent-windows app launch Notepad
agent-windows observe
agent-windows type "Hello, World!"
agent-windows shortcut ctrl+s
agent-windows type "test.txt"
agent-windows shortcut enter
```

### Fill a form

```bash
agent-windows observe                    # Find form fields in snapshot
# Type into field at @e2 (Edit, Position (100,200,300,230))
agent-windows type "John" --at 200,215
# Type into field at @e3 (Edit, Position (100,250,300,280))
agent-windows type "john@email.com" --at 200,265
# Click submit button at @e5 (Button, Position (400,350,500,385))
agent-windows click 450 367
agent-windows observe                    # Verify submission
```

### Navigate a multi-level menu

Some menus have icon-only buttons with no text. Use a mixed approach:

```bash
agent-windows observe                    # UIA tree for named elements
agent-windows ask-vision "Describe the toolbar icons in the top panel"
agent-windows ground "the print icon"    # Approximate coordinates
agent-windows click 350 42               # Click with vision-guided coords
agent-windows observe                    # Re-snapshot for new menu items
```

### Wait for a dialog and handle it

```bash
agent-windows click 500 300              # Trigger action that opens dialog
agent-windows wait --text "Confirm" --timeout 10
agent-windows observe                    # Find Confirm/Cancel buttons
agent-windows click 450 320              # Click Confirm
```

## Reference Files

For detailed information beyond this guide:

| File | When to read |
|------|-------------|
| `references/commands.md` | Full parameter reference for every command |
| `references/selectors.md` | Deep dive on @eN selectors, snapshot format, coordinate system |
| `references/vision.md` | ground/detect/ask-vision details, vision model configuration |
| `references/configuration.md` | Config file reference, env vars, daemon management |
| `references/troubleshooting.md` | Common failures, error codes, debug mode |
