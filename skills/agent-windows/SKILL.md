---
name: agent-windows
description: "Windows desktop automation CLI for AI agents. Use when the user wants to automate anything on a Windows desktop: open apps, read the screen, click, type, press shortcuts, scroll, drag, run PowerShell, or manage files, processes, windows, and clipboard. Triggers: 'open/launch an app', 'click the button', 'type into the field', 'take a screenshot', 'read the screen', 'run this PowerShell', or any Windows GUI automation task."
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
3. agent-windows click @eN            # Act via selector (preferred)
   # or: extract coords from Position, compute center, click cx cy
4. agent-windows observe              # Verify the result
```

**Critical rule**: `@eN` refs from the Interactive Elements table go **stale** after any UI change (click, type, shortcut). Always re-observe before the next interaction.

## Quickstart

```bash
# Initialize config (first time only)
agent-windows config init

# Tip: --config <path> runs against an alternate config.toml (propagated to the
# daemon; state files stay put). Use for isolated/test profiles:
#   agent-windows --config /tmp/ci.toml mcp serve

# See the screen
agent-windows observe

# Click a button (find @e3 in snapshot)
agent-windows click @e3               # via selector (preferred)
agent-windows click 450 320           # or explicit coordinates

# Type text
agent-windows type "Hello World" --enter

# Keyboard shortcut
agent-windows shortcut ctrl+s
```

## Reading the Screen

Three observation tools, each suited to different needs:

### observe — Combined (snapshot + screenshot)

```bash
agent-windows observe                    # snapshot + screenshot (always both)
agent-windows observe --annotate         # + numbered @eN boxes on the screenshot
agent-windows observe -d 0               # scope both to display 0
```

Output includes: Desktop State (cursor, resolution), Windows table, Accessibility Tree, Interactive Elements table. For an individual piece use `snapshot`/`screenshot` directly; for visual grounding use `ground`.

### snapshot — UIA Tree Only

```bash
agent-windows snapshot                           # full screen (default)
agent-windows snapshot --window "记事本"          # scope by title substring
agent-windows snapshot --window notepad           # ...or process name (title→process fallback)
agent-windows snapshot --window 0x1a2b            # ...or HWND (decimal or 0xHEX)
agent-windows snapshot --active                   # foreground window only
```

Returns a structured text view of the UIA accessibility tree. Contains the **Interactive Elements table** with `@eN` refs and their bounding box coordinates. This is the primary way to find clickable targets.

`--window` / `--active` scope the traversal to a single top-level window — use when one large window (browser, IDE, complex dialog) would otherwise flood the output. `--window V` resolves as HWND (`12345` / `0x1a2b`), title substring (case-insensitive), then process name, mirroring `app switch`; first match wins and a warning is printed when multiple match. In single-window mode `@eN` restarts at `@e1` and maps to that window's subtree, written to the same 2s cache slot — so an immediate `click @e1` / `get-text @e1` lines up with the subtree, no cross-window mapping. `--window` and `--active` are mutually exclusive.

**Related**: `snapshot-diff` diffs a stored baseline UIA tree against the current desktop, reporting added / removed / changed elements — useful for detecting state drift between runs (distinct from the pixel-level `diff`). See `references/commands.md` for the flag list.

### list-displays — Enumerate Displays

```bash
agent-windows list-displays            # table: index, primary, resolution, rect, work area, device
```

Lists every monitor so you can pick the right `-d N` for `snapshot`/`observe`/`screenshot` instead of guessing. Indices are 0-based and match the `-d`/`--display` filter exactly (same `EnumDisplayMonitors` order). `*` marks the primary display; negative `Rect` coordinates mean a monitor left/above the primary (multi-monitor desktop). Add `--format json` (as a global prefix flag) for a structured `displays[]`/`count`/`dpi_scale`.

### screenshot — Screen Capture

```bash
agent-windows screenshot                 # Full screen
agent-windows screenshot -s @e3          # Crop to UIA element
agent-windows screenshot --bbox 100,200,500,400  # Crop to region
agent-windows screenshot -o shot.png     # Save to file
agent-windows screenshot --annotate      # Overlay numbered @eN boxes (full screen by default)
agent-windows screenshot --annotate --grid-w 4 --grid-h 4  # Annotated + % reference grid
```

> `--annotate` draws a numbered box per interactive element from a fresh snapshot, so the `@eN` labels line up with the current `snapshot`/`observe` table. With no scope it captures the **full virtual screen** (all displays); pass `--bbox`/`-s`/`--monitor` to annotate just that region (boxes translate into the scoped capture). Use `--grid-w N`/`--grid-h N` to draw `N` evenly-spaced reference lines with `X%`/`Y%` labels to sanity-check coordinates.

### Decision Matrix

| Tool | Precision | Best for |
|------|-----------|----------|
| `snapshot` | Pixel-exact | Named controls, dialogs, menus, standard UI |
| `ground` / `detect` | ±~100 px | Custom-drawn UI that UIA can't capture |
| `ask-vision` | Descriptive only | Understanding icons, reading screen state |

Prefer `snapshot` for element location; reach for vision only when UIA can't see an element (see `references/vision.md`).

### Template matching (`find-image`) — deterministic, free

When you already have an image of the target (an icon, a glyph, a status indicator), `find-image` finds it by pixel match — no vision-model call, no API cost, fully deterministic. Prefer it over `ground` when a template exists.

```bash
# Find a saved icon and click its center
agent-windows find-image save.png
# → center coords ready for `agent-windows click X Y`

# Tolerate anti-aliasing / minor DPI differences
agent-windows find-image icon.png --tolerance 10 --grayscale

# Find every occurrence (count icons)
agent-windows find-image checkbox.png --all

# Restrict the search to a window region for speed
agent-windows find-image btn.png --bbox 100,100,800,600
```

Capture a template once with `agent-windows screenshot --bbox x1,y1,x2,y2 -o icon.png` (crop tight to the target, at native DPI). Templates are capped at 128×128; for larger targets, crop with `--bbox` first.

### Color sampling (`pixel-at`) — judge state by color

When state shows as color — an LED flipping green/red, a progress bar's fill, a selected swatch — `pixel-at` reads the color at a point and can compare it to a target. Deterministic, no vision-model call. Prefer it over `diff` (whole-image) or `ground`/`detect` (vision) when one pixel's color is the signal.

```bash
# Is the status LED green? → is_match boolean (exact)
agent-windows pixel-at 100 200 --match "#00cc00"

# Tolerate anti-aliasing + judge by hue only
agent-windows pixel-at 100 200 --match "#00cc00" -t 10 --grayscale

# Just read the color (no match): returns hex + rgb
agent-windows pixel-at 100 200

# Average a 5x5 box — robust on anti-aliased / high-DPI edges
agent-windows pixel-at 100 200 --sample 5
```

`--match COLOR` accepts `#RRGGBB` / `#RGB` / `rgb(r,g,b)` / bare hex; `is_match = (max per-channel diff <= -t tolerance)`. Find the point's coords first with `snapshot`/`screenshot --annotate`, or reuse a `find-image`/`ground` result's `x, y`. For a live capture the point is in virtual-screen coords (same space as `click`); pass `--in FILE` to sample a saved screenshot instead.

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
| Ref | Type    | Action       | Name     | Window   | Position           | State   |
|-----|---------|--------------|----------|----------|--------------------|---------|
| @e1 | Button  | click        | OK       | Settings | (400,300,500,340)  |         |
| @e2 | Edit    | fill         | Search   | Settings | (100,100,300,130)  | focused |
| @e3 | Button  | click        | Cancel   | Settings | (520,300,620,340)  |         |
| @e4 | CheckBox| toggle       | Remember | Settings | (400,360,500,390)  | toggle_state:on |
| @e5 | ComboBox| select/expand| Font     | Settings | (100,200,300,230)  | expand_collapse_state:collapsed |
```

The **Action** column tells you the verb(s) the element supports, so you know immediately what to do with `@eN` without trial-and-error:
- `click` — buttons, menu items, hyperlinks, and anything with no other action (→ `click`)
- `fill` — text fields, search boxes (→ `type`)
- `toggle` — checkboxes (→ `click` flips on/off)
- `select` — radio buttons, list/tree items, tab items, combo boxes (→ `click`)
- `slide` — sliders, spinners, scroll bars — drag the thumb (→ `drag`)
- `scroll` — scrollable documents/containers (→ `scroll`)
- `expand` — expandable combo boxes, collapsible groups, tree items (→ `expand`/`collapse`)

Multiple verbs join with `/` when an element supports more than one (e.g. `select/expand` for a combo box, `scroll/expand` for a scrollable collapsible group).

The State column also surfaces richer attributes when the element exposes them: `toggle_state:on|off|indeterminate`, `value:...`, `expand_collapse_state:expanded|collapsed|partially expanded` (combo boxes, tree items, menus, collapsible groups), `scrollable:v|h|vh` + `scroll_pct:v:42%` (scrollable containers), `min`/`max` (sliders), `is_selected:true|false` (list/radio/tab items), `is_password:true`, `shortcut:Ctrl+S`, `help_text:...`. Use these to disambiguate similar-looking elements and read current state (e.g. a checkbox's on/off) without an extra click. Scrollable and expandable containers are promoted to interactive (they get an `@eN` ref) so they can be targeted by `expand`/`collapse`/`scroll`.

### Commands That Accept @eN Directly

These commands handle coordinate extraction internally:

```bash
agent-windows get-text @e3               # Returns element's name/text
agent-windows is visible @e3             # Check if element is on-screen
agent-windows is enabled @e3             # Check if element is enabled
agent-windows wait @e3 --timeout 10      # Wait for element to appear
agent-windows screenshot -s @e3          # Screenshot cropped to element
agent-windows click @e3                  # Click element's bbox center
agent-windows type "text" --at-selector @e3   # Click element center, then type
agent-windows scroll @e3 --direction up
```

`click`, `dblclick`, `hover`, `move`, `mouse-down`, `mouse-up`, and `scroll` accept an `@eN` ref as a bare positional (e.g. `click @e3`) and resolve the element's bbox center for you — no manual coordinate math. `type` takes `--at-selector @e3` (clicks the center, then types).

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
agent-windows click @e3 --background    # No-focus-steal click (UIA Invoke; left single only)
agent-windows hover 500 300                # Hover (trigger tooltips)
agent-windows move 500 300                 # Move mouse only
agent-windows scroll 500 300 --direction down --amount 5   # Scroll
agent-windows drag 100 200 400 500         # Drag from A to B
agent-windows mouse-down 500 300           # Press and hold
agent-windows mouse-up 500 300             # Release
```

`click`/`dblclick`/`hover`/`move` self-verify by default: each appends `Hit:` (the element under the cursor) and `Focus:` (the foreground window) to its output, and `drag` appends `Drop:` + `Slider:`. So you usually **don't need a follow-up `snapshot`** to confirm a click landed on the right thing — read the verify lines. Turn it off for raw speed with `config set input.verify false`.

`--background` delivers the click via the UIA `Invoke` / `Toggle` / `Select` pattern instead of `SendInput`, so the target window never comes to the foreground — handy for poking a non-foreground or UWP window without stealing focus. Left single-click only; if the element has no invokable pattern (or no host HWND) the call raises `-1007 BACKGROUND_UNAVAILABLE` — retry foreground.

### Keyboard

```bash
agent-windows type "Hello World"                     # Type text (Unicode, bypasses IME)
agent-windows type "search term" --at 300,200        # Click field first, then type
agent-windows type "text" --at-selector @e3          # Click element @e3, then type (preferred)
agent-windows type "text" --enter                    # Press Enter after typing
agent-windows type "new text" --clear                # Select-all + delete first
agent-windows type "prefix" --caret end              # Move caret before typing: start|idle|end
agent-windows shortcut ctrl+c                        # Copy
agent-windows shortcut ctrl+v                        # Paste
agent-windows shortcut alt+f4                        # Close window
agent-windows shortcut win+r                         # Run dialog
agent-windows key-down a --hold 0.5                  # Hold key 0.5s then auto-release
agent-windows key-down shift                         # Press and HOLD (stays down)
agent-windows key-up shift                           # Release (pair with key-down)
```

Key notation for `shortcut`: `ctrl`, `alt`, `shift`, `win` as modifiers, joined with `+`. Special keys: `enter`, `escape`, `tab`, `space`, `backspace`, `delete`, `insert`, `printscreen`, `apps` (context-menu key), `up/down/left/right`, `home`, `end`, `pageup`, `pagedown`, `f1`–`f12`. Common aliases are also accepted (`control`, `windows`/`super`, `return`, `esc`, `back`, `del`, `pgup`/`pgdn`, `prtsc`, `menu`/`context`). The same notation applies to `key-down`/`key-up` (raw key press/release; `--hold` on `key-down` auto-releases after N seconds, otherwise the key stays pressed until `key-up`).

`type` options: `--at X,Y` or `--at-selector @eN` (click first; `--at-selector` overrides `--at`), `--enter`, `--clear` (select-all + delete first), `--caret start|idle|end` (move caret before typing — `start` presses Home, `end` presses End, `idle` leaves it; applied before `--clear`).

`type` also self-verifies: it reads the field's value before and after injecting and appends a git-diff-style `Field value:` line (or a `Field value diff:` block for multi-line fields), so you can confirm the text actually landed without a follow-up `get-text`/`snapshot`. `Field value: no change` means the input was rejected, maxlength-truncated, or IME-swallowed; password fields show `(password field, value hidden)` and never leak the value. Same `input.verify` toggle.

### Expand / Collapse

Drive an element's `ExpandCollapsePattern` — combo boxes, tree items, menus, collapsible groups. Call `snapshot` first to find a node whose State shows `expand_collapse_state:collapsed` (or `expanded`), then target its `@eN` ref. Both are idempotent: expanding an already-expanded/leaf node is a no-op; same for collapsing.

```bash
agent-windows expand @e3       # Open a combo box / tree node
agent-windows collapse @e3     # Close it again
```

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
agent-windows shell "Get-Process" --timeout 60   # override default 30s (shell.timeout)

# Filesystem
agent-windows fs list C:\Users
agent-windows fs read C:\Users\test\file.txt
agent-windows fs read legacy.txt --encoding gbk        # legacy codepage (default UTF-8)
agent-windows fs write C:\Users\test\out.txt "Hello"
agent-windows fs copy C:\a.txt C:\bak\a.txt            # --overwrite to replace
agent-windows fs move C:\old.txt C:\new.txt
agent-windows fs delete C:\temp\junk --recursive       # → Recycle Bin by default (reversible); non-empty dirs need --recursive
agent-windows fs delete C:\temp\junk --permanent        # force-delete, bypass Recycle Bin
agent-windows fs delete C:\temp\junk --dry-run          # preview what would be deleted; nothing removed
agent-windows fs search C:\Projects "*.py"             # glob, recursive by default
agent-windows fs info C:\Users\test\file.txt           # size/timestamps/attrs

# Clipboard
agent-windows clipboard get                  # Read clipboard
agent-windows clipboard set "copied text"    # Write to clipboard
agent-windows clipboard get --keyboard       # Simulate Ctrl+C first
agent-windows clipboard set "text" --keyboard # Simulate Ctrl+V after

# Process
agent-windows process list
agent-windows process list --sort-by cpu --limit 20    # memory|cpu|name; --name filters
agent-windows process kill 1234              # By PID
agent-windows process kill notepad           # By name
agent-windows process kill notepad --force   # 强杀 immediately (default: graceful)

# Screenshot cleanup — delete old/oversized screenshots now (otherwise runs
# automatically every screenshot.cleanup_interval_minutes)
agent-windows cleanup
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

### wait-for — Condition Polling

`wait-for` waits until a multi-condition UI predicate is satisfied — more expressive than `wait`, which only handles a single selector or text match. Polls the accessibility tree until the condition holds or the timeout elapses.

```bash
agent-windows wait-for text_exists --text "完成"              # any visible text
agent-windows wait-for active_window --window "Notepad"       # active window title
agent-windows wait-for element_exists --text OK --timeout 10  # an element exists
agent-windows wait-for enabled --text Submit                   # ...and is enabled (alias)
agent-windows wait-for focused --text "Search"                 # ...and has focus (alias)
```

Conditions: `text_exists`, `active_window`, `element_exists`, `element_enabled`, `focused_element` (short aliases `text`/`window`/`element`/`enabled`/`focused` also accepted). **Matching is case-sensitive substring** — match the exact case shown in `snapshot` output. Like `wait`, raises `-1003` (WAIT_TIMEOUT) on timeout, `-32602` (INVALID_PARAMS) for an unknown condition. See `references/commands.md` for the full parameter table.

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

### Notifications

```bash
agent-windows notify "Build complete" "All tests passed"   # Windows toast (title + body)
agent-windows notify "Alert" "Disk 95% full" --app-id my.app  # Custom AppUserModelID
```

Shows a Windows toast via PowerShell WinRT (`ToastNotificationManager`). The default app id is PowerShell's own AUMID (always present, no registration needed); a custom `--app-id` only displays reliably if a matching Start Menu shortcut is registered. Title/message are XML-escaped; `app_id` must not contain a single quote.

### Registry

Read and write the Windows Registry (PowerShell-drive paths like `HKCU:\Software\MyApp`):

```bash
agent-windows registry list HKCU:\Software\Microsoft\Windows        # Values + sub-keys
agent-windows registry get HKCU:\Software\MyApp Version             # Read a value
agent-windows registry set HKCU:\Software\MyApp Count 42 -t DWord   # Set typed value
agent-windows registry delete HKCU:\Software\MyApp Version          # Delete value
agent-windows registry delete HKCU:\Software\MyApp                  # Delete whole key
```

`set` types: `String` (default), `ExpandString`, `DWord`, `QWord`, `MultiString` (value = JSON array), `Binary` (value = hex string). Paths must start with `HKCU/HKLM/HKCR/HKU/HKCC/HKPD`.

### Power

Control system power state. Use `lock` to secure the workstation (Win+L, recoverable by logging back in). Use `sleep` to suspend to S3 (recoverable by waking). `shutdown` and `restart` are irreversible; both accept `--delay N` (0–600 seconds, default 0 = immediate) and `--force` (skips save dialogs, apps lose unsaved data). A scheduled shutdown (`--delay N` where N > 0) can be canceled by the user via `shutdown /a` or the Windows UI.

```bash
agent-windows lock                    # Lock workstation
agent-windows sleep                   # Suspend to S3
agent-windows shutdown                # Immediate shutdown
agent-windows shutdown --delay 120    # 2-minute delay
agent-windows shutdown --force        # Skip save dialogs
agent-windows restart --delay 60 --force
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

## When Things Go Wrong

A command failed? Don't guess — diagnose in order:

1. **Read the error code** first. Common ones:
   - `-1001 ELEMENT_NOT_FOUND` → `@eN` ref is stale; re-`observe` and use a fresh ref.
   - `-1002 COORDINATE_OUT_OF_RANGE` → click/scroll coordinates outside the screen; re-`observe` for fresh bounds.
   - `-1003 WAIT_TIMEOUT` → element/text never appeared (check the name's exact case — matching is case-sensitive), or a `shell` command exceeded `shell.timeout`; raise the timeout.
   - `-1004 PERMISSION_DENIED` → UAC blocked the action, a higher-integrity (elevated) target blocked by UIPI, or a `shell` command matched `shell.blacklist`.
   - `-1005 VISION_API_ERROR` → vision model not configured/rate-limited; run `config doctor`.
   - `-1007 BACKGROUND_UNAVAILABLE` → a `--background` click found no invokable pattern or no host window, or `expand`/`collapse` had no host window; retry foreground (click) or re-`snapshot`. (A non-expandable element raises `-32602`, not `-1007`.)
   - Standard JSON-RPC codes (`-32600`…`-32603`) and the full table are in `references/troubleshooting.md`.

2. **Re-observe** (`agent-windows observe`) to see the current screen state — it usually reveals what changed.

3. **For full diagnostics**: add `--debug --format json` to any command. `--no-daemon` shows full Python stack traces.

For the complete error-code table and per-symptom recovery steps, see `references/troubleshooting.md`.

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
