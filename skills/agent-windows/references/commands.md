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

> **Windows table** (`# | Title | App | Status | Position`): the `Status` column reports the window's placement — `Maximized`, `Minimized`, or `Normal` — and appends `(active)` for the foreground window (e.g. `Normal (active)`). A `Minimized` window's `Position` is its icon rectangle, not a usable client area, so don't click into it from those coordinates — restore/switch to it first. The `App` cell appends `(browser)` when the process is a known web browser (chrome/msedge/firefox), signalling that the window renders a web page — prefer keyboard shortcuts or `@eN` tree refs over coordinate guesses on its content.

### snapshot

UIA accessibility tree traversal. Returns the full structured text view.

```bash
agent-windows snapshot                            # full desktop (default)
agent-windows snapshot -d 0                        # only the primary display
agent-windows snapshot -d 0 -d 1                   # union of displays 0 and 1
agent-windows snapshot --window "记事本"            # scope by title substring
agent-windows snapshot --window notepad             # ...or process name (title→process fallback)
agent-windows snapshot --window 0x1a2b              # ...or HWND (decimal or 0xHEX)
agent-windows snapshot --active                     # foreground window only
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `--display` / `-d` | list[int] | `[]` | Limit windows/interactive-elements/cursor to the given display index(es), repeatable (e.g. `-d 0 -d 1`). Omit for the full desktop. |
| `--window` | str | `""` | Scope to one top-level window. `VALUE` is an HWND (decimal `12345` or `0xHEX`), a title substring (case-insensitive), or a process name — title misses fall back to process match, same as `app switch`. First match wins; multiple matches print a `⚠ Matched N windows ...` warning at the top (pass the HWND for determinism). Zero matches → `INVALID_PARAMS` (`Window not found`). |
| `--active` | bool | `false` | Scope to the foreground window (`GetForegroundWindow`). Mutually exclusive with `--window` (both given → `INVALID_PARAMS`). |

Uses config `tree.max_depth` (default 15), `tree.max_elements` (default 200), `tree.timeout` (default 10s), `tree.include_invisible`, `tree.include_offscreen`.

**Window scoping** (`--window` / `--active`): skips the full-screen enumerate and expands only the target window's subtree — lighter output when one large window (browser/IDE) would otherwise dominate. `@eN` refs restart at `@e1` and are written to the same 2s TTL cache slot used by `observe`/`snapshot`, so a follow-up `click @e1` / `get-text @e1` / `element_state @e1` resolves against that subtree with no cross-window mapping. Output is prefixed with a `## Window: "<title>" (<process>, HWND 0x...)` header (plus the `⚠ Matched ...` line when the selector matched more than one window). All `tree.*` caps (`max_depth` / `max_elements` / `timeout`) still apply to the subtree.

### list-displays

Enumerate every display so you know which `-d N` to pass to `snapshot`/`observe`/`screenshot` instead of guessing.

```bash
agent-windows list-displays
agent-windows --format json list-displays   # structured output (global flag, prefix position)
```

No parameters. Indices are 0-based, `EnumDisplayMonitors` order — **identical to the `-d N` / `--display N` filter**, so the `N` shown here is the `N` you pass to `-d`.

**Output**: a `## Displays (count)` markdown table — `#` (index), `Primary` (`*` marks the primary display), `Resolution` (`WxH`), `Rect (L,T,R,B)` (full screen rect), `Work Area (L,T,R,B)` (desktop minus taskbar), `Device` (raw `\\.\DISPLAYN`). A `System DPI scale:` footnote follows. With `--format json`, returns `{count, displays[], dpi_scale, output}` where each entry also carries the derived `width`/`height`.

**Display filtering**: `-d` clips each window/element bounding box to the selected display(s)' union and drops items that don't overlap (intersect-clip, not "window belongs to one display" — a window spanning two displays is kept, clipped to the selected one). `@eN` refs are renumbered against the filtered list. The cursor is reported as `N/A` when it's off the selected display(s). The Accessibility Tree text block is NOT filtered (it's free text without per-line coordinates); a note is shown when a filter is active.

**Output**: Same as observe but without screenshot file. Contains the Interactive Elements table with `@eN` refs.

**Action column**: each element's Action column shows the verb(s) it supports — `click` (buttons/menu items/hyperlinks), `fill` (text fields → `type`), `toggle` (checkboxes), `select` (radios/list items/tree items/tab items/combo boxes), `slide` (sliders/spinners/scroll bars — drag the thumb → `drag`), `scroll` (scrollable documents/containers), `expand` (expandable combos/collapsible groups/tree items). Multiple verbs join with `/` (e.g. `select/expand` for a combo box, `scroll/expand` for a scrollable collapsible group). This tells you what to *do* with `@eN`; the State column tells you its *current* state. Derived from control type + cached UIA properties, so it's free (no extra COM round-trips).

**Rich element attributes**: the Interactive Elements table's State column surfaces extra attributes when the element exposes them — `toggle_state:on|off|indeterminate` (checkboxes/toggles), `value:...` (text fields, sliders), `expand_collapse_state:expanded|collapsed|partially expanded` (combo boxes, tree items, menus, collapsible groups — any element exposing the state), `scrollable:v|h|vh` + `scroll_pct:v:42% h:0%` (scrollable containers), `min:..`/`max:..` (sliders), `is_selected:true|false` (list/radio/tab items), `is_password:true`, `shortcut:Ctrl+S`, `help_text:...`. These are read from the cached UIA properties (no extra COM round-trips) and help disambiguate similar-looking elements and read current state without an extra click. Note: scrollable and expandable containers are promoted to interactive (they get an `@eN` ref) so they can be targeted by `expand`/`collapse`/`scroll`. Long element names and attribute values are truncated in the table with a `...(+N)` suffix reporting how many characters were dropped — if you need the full text, follow up with `get-text @eN`.

### screenshot

Capture a screenshot, optionally cropped to a region or UIA element, or annotated with detection boxes + a reference grid.

```bash
agent-windows screenshot [-o PATH] [--monitor N] [-s @eN] [--bbox X1,Y1,X2,Y2]
agent-windows screenshot --annotate                    # number every element @eN
agent-windows screenshot --annotate --grid-w 3 --grid-h 3   # + 3×3 reference grid
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `--output` / `-o` | str | `""` | Save to file path |
| `--monitor` | int | `-1` | Monitor index (-1 = all monitors) |
| `--selector` / `-s` | str | `""` | UIA element ref (e.g. `@e3`) |
| `--bbox` | str | `""` | Crop region: `x1,y1,x2,y2` |
| `--annotate` | bool | `false` | Overlay numbered `@eN` detection boxes for each interactive element (requires full screen; ignores `--bbox`/`--selector`/`--monitor`) |
| `--grid-w` | int | `0` | With `--annotate`: draw N vertical reference lines with X% edge labels |
| `--grid-h` | int | `0` | With `--annotate`: draw N horizontal reference lines with Y% edge labels |

`--selector` resolves the element's bounding box and crops to it. `--bbox` specifies pixel coordinates directly. If both given, `--selector` takes priority.

`--annotate` draws a numbered box around each interactive element matching the snapshot's `@eN` refs, so you can visually map a ref to its on-screen location. Add `--grid-w`/`--grid-h` to overlay a reference lattice with percentage labels at each line — useful for estimating coordinates when reasoning about element positions. **`--grid-w`/`--grid-h` only take effect with `--annotate` — they are ignored (silently) otherwise.** Annotation requires the full-screen capture (boxes use screen coordinates), so a `--bbox`/`--selector`/`--monitor` is dropped with a warning when `--annotate` is set.

**Output**: `{output, path, width, height, coord_scale, backend, region}`

> **MCP tool only** — the `screenshot` MCP tool additionally takes `scale` (float, default `screenshot.scale` = `1.0`), `max_width` (int, default `screenshot.max_width` = `1920`), and `max_height` (int, default `screenshot.max_height` = `1080`) to downscale the capture for cheaper vision-model input. It also takes `display` (int or list[int]) to crop to the union of the selected display(s) — same semantics as the snapshot/observe `display` filter (e.g. `display=[0, 1]` for the union of displays 0 and 1); a bad index raises an error. `display` resolves to a bbox and overrides `bbox` when both are given. None of these are CLI flags; set the scale/dimensions via the `[screenshot]` config or override per-call over MCP.

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

**Output**: `{output, description, count, targets, screenshot_path}`. Each target: `{index, label, box: {x1, y1, x2, y2}, center_x, center_y}` — `center_x`/`center_y` is the bbox midpoint (click-ready, no need to compute it).

### find-image

Locate a saved template image on screen and return click-ready coordinates.
Deterministic, zero-API-cost alternative to `ground`/`detect`.

**Usage:** `find-image TEMPLATE [--in SOURCE] [--bbox X1,Y1,X2,Y2] [--tolerance N] [--grayscale] [--all]`

- `TEMPLATE` (required): path to the template image to locate.
- `--in SOURCE`: search a saved screenshot instead of a fresh capture.
- `--bbox X1,Y1,X2,Y2`: restrict the search region (faster; coords offset back automatically).
- `--tolerance N` / `-t N`: per-channel max abs diff, 0-255 (0 = exact).
- `--grayscale`: match in grayscale (~3× faster, ignores color).
- `--all`: return every match (NMS-deduped) instead of just the best.

Returns `{count, matches:[{x,y,w,h,center_x,center_y,score}], method:"template", screenshot_path}`. Single-match mode also flattens the best match's center to top-level `x, y`. `method` is always `"template"` (vs `ground`'s `"visual-*"`). Templates are capped at 128×128; for larger, crop with `--bbox` first.

### pixel-at

Read the color at a screen point; optionally compare it to a target color.
Deterministic, zero-API-cost — use to judge state by color (an LED that flips
green/red, a progress bar's fill, a selected swatch) without a vision-model call.

**Usage:** `pixel-at X Y [--in SOURCE] [--sample N] [--match COLOR] [-t N] [--grayscale] [--output PATH]`

- `X Y` (required): the point. Virtual-screen px for a live capture (same space
  as `click`); image px with `--in`. Integers (negative ok on multi-monitor).
- `--in SOURCE`: read from a saved screenshot instead of a fresh capture.
- `--sample N`: average an N×N box centered on (X,Y) (default 1). Robust against
  anti-aliasing / high-DPI jitter; clamps at screen edges.
- `--match COLOR`: target color to compare — `#RRGGBB` / `#RGB` / `rgb(r,g,b)` /
  bare hex. When given, the result includes an `is_match` boolean.
- `--tolerance N` / `-t N`: per-channel max abs diff for the match, 0-255 (0 = exact).
- `--grayscale`: compare luminance instead of RGB (both sides converted).

Returns `{x, y, rgb:[r,g,b], hex:"#rrggbb", sample, method:"pixel", screenshot_path}`.
With `--match`, also `match:{target, tolerance, is_match, max_diff, score}` where
`is_match = (max per-channel diff <= tolerance)` and `score = 1 - max_diff/255`.
`screenshot_path` is empty unless `--output` is given (no auto-save — pixel-at is
a lightweight, frequently-polled sensor).

```bash
# Is the status LED green? (exact match)
agent-windows pixel-at 100 200 --match "#00cc00"

# Same, tolerating anti-aliasing + judge by hue only
agent-windows pixel-at 100 200 --match "#00cc00" -t 10 --grayscale

# Just read the pixel color (no match)
agent-windows pixel-at 100 200

# Robust sample over a 5x5 box (averages anti-aliased edges)
agent-windows pixel-at 100 200 --sample 5
```

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

**Output**: `{output, path, regions}` — `regions` is a list of bbox dicts (`x1/y1/x2/y2/label/color`); output renders each as `(x1,y1)-(x2,y2)`. Sibling is daemon-direct-client only: MCP surfaces only `output`; the CLI's default text rendering uses `output`; `--format json` (a daemon-direct client) surfaces the structured siblings.

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

**Output**: `{output, diff_pixels, total_pixels, diff_percent, match, changed_bbox, diff_path}` — `changed_bbox` is the exclusive `[left, top, right, bottom]` of all changed pixels (`None` when `match`); output adds a `Changed region: (x1,y1)-(x2,y2)` line (or `none`). Sibling is daemon-direct-client only: MCP surfaces only `output`; the CLI's default text rendering uses `output`; `--format json` (a daemon-direct client) surfaces the structured siblings.

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

> **Negative coordinates** are supported on both the native launcher and the Python CLI (`agent-windows-python` / `--no-daemon`). A multi-monitor desktop extends left or above the primary monitor into negative space (e.g. a window at `x=-1920`), so `agent-windows click -1920 0` and `agent-windows drag -1920 0 -100 100` work without quoting or `--` separators.

> **Post-op verify (default on).** `click`, `dblclick`, `hover`, and `move` append two best-effort lines after the op so you usually don't need a follow-up `snapshot` to confirm what you hit:
> - `Hit: <Type> "<name>"` — the UIA element under the cursor after the op (`(none)` if it landed on dead space / non-client area). Names truncate to 40 chars with a `...(+N)` tail marker.
> - `Focus: "<window title>"` — the current foreground window (changes when the op opens a modal / new window; omitted if empty).
>
> These are decoration: any UIA read failure degrades to a missing line, never an error. Turn off for raw speed with `config set input.verify false` (live; the daemon re-reads config per command).
>
> The `Hit:`/`Drop:`/`Slider:` reads use UIA `ElementFromPoint`, which returns the **topmost element on screen at that point (visual z-order)** — the same target the physical click actually struck. If another window covers that coordinate, the read reports the *covering* window, not the intended one (foreground *focus* ≠ visual *topmost*). That mismatch surfaces the obscuring instead of silently misreporting, but there is no coordinate-free guarantee — when a `Hit:`/`Drop:` names an unexpected window, re-`snapshot` and retry with the covering window moved aside.

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

**Post-op verify (default on).** `drag` appends best-effort lines about the destination:
- `Drop: <Type> "<name>"` — the element hit at the end point (the drop target).
- `Slider: <N>%` — if that element exposes a `RangeValue`/`Scroll` pattern, its post-drag percentage (native Win32/WPF sliders report this; scrollbars and custom canvas sliders often don't — best-effort, omitted when unavailable).

Same fault-tolerance and `[input] verify` toggle as the other mouse commands.

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

### expand / collapse

Expand or collapse an expandable UI element at `@eN` (a ComboBox dropdown, TreeItem, menu, or collapsible Group). Unlike the coordinate-based input commands, these drive the element's UIA `ExpandCollapsePattern` directly — call `snapshot` first to find a node whose State shows `expand_collapse_state: collapsed` (or `expanded`), then pass its `@eN`. Both are idempotent: an already-expanded element is skipped on `expand`, already-collapsed (or a leaf) on `collapse`. `expand` also skips a `leaf` (nothing to expand); a `partially expanded` node is expanded further (the provider decides). If the element has moved or been obscured since the snapshot, the error tells you to re-snapshot.

```bash
agent-windows expand @e4
agent-windows collapse @e4
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `SELECTOR` | str | **required** | Element ref (`@eN`) |

**Output**: `{output}` with the element, resulting State, and whether the action was skipped.

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

> **Length cap**: `text` is capped at **10000 characters**. Typing is char-by-char with a per-key delay, so an oversized string would block the single worker thread for minutes/hours — a larger payload is rejected with `INVALID_PARAMS`. To enter a long document, write it to a file with `fs write` and paste it, or type it in chunks.

> **Post-op field-value verify (default on).** `type` reads the target field's value before and after injection and appends a git-diff-style block, so you usually don't need a follow-up `snapshot`/`get-text` to confirm the text actually landed:
> - Single-line change: `Field value: <old> → <new>` (empty string if the field was empty before).
> - Multi-line change: a `Field value diff:` block with `+`/`-` lines (only the changed lines, each truncated to 120 chars).
> - `Field value: no change` — the input was rejected, maxlength-truncated, or IME-swallowed.
> - `Field value: (password field, value hidden)` — password fields never echo the value.

> The `--at`/`--at-selector` path reads by coordinate (`ControlFromPoint` — visual z-order topmost, same obscuring caveat as the `Hit:` line: if another window covers the point, pre/post report the covering field, surfacing the miss), so pre and post are guaranteed the same field even though focus may be elsewhere before the click; the no-coords path reads the focused field (pre is read **before** `--clear`, so `--clear` shows old→new, not empty→new). Non-text controls exposing no value pattern surface nothing. Same fault-tolerance and `[input] verify` toggle as the mouse commands (`config set input.verify false` for raw speed). A structured `field_value: {pre, post, is_password}` sibling is also returned (pre/post truncated to 500 chars; visible only to direct daemon/IPC clients — MCP and CLI consumers read the `output` block).

> **Click-target echo.** With `--at` or `--at-selector`, the output reports the resolved click point — `At: (x, y)` for `--at`, or `At: @eN (x, y)` for `--at-selector` — plus a `Hit:`/`Focus:` verify of what the click landed on (same as the `click` command, default-on; off when `[input] verify = false`). Without a click target neither line appears. The typed text itself is not echoed back; read the `Field value` line above to see what landed in the field.

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

### key-down / key-up

Press and hold / release keys as raw key events. Use the pair together for custom hold sequences, or pass `--hold` to `key-down` for a self-contained timed press (press → sleep → auto-release).

```bash
agent-windows key-down a --hold 0.5          # Hold 0.5s then auto-release
agent-windows key-down ctrl+shift+a          # Press and HOLD (stays down)
agent-windows key-up ctrl+shift+a            # Release (pair with key-down)
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `keys` | str | **required** | Key notation, e.g. `a`, `ctrl+shift+a`, `enter` (same vocabulary as `shortcut`) |
| `--hold` | float | `None` | Hold N seconds then auto-release (max 60); omit to keep pressed (pair with `key-up`) |

Same key vocabulary as `shortcut`. `key-up` takes only `keys`. Without `--hold`, keys stay pressed until a matching `key-up` — a stuck key corrupts later input, so always pair them.

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

Launch an application. By name (resolved via the Start Menu, so **friendly names work — including UWP/Store apps** like `calculator`, `edge`, `photos`), by executable name on PATH, or by full path. Trailing tokens after the name/path are passed as arguments.

```bash
agent-windows app launch Notepad
agent-windows app launch calculator              # UWP/Store app by friendly name
agent-windows app launch "C:\Program Files\app\test.exe" --flag value
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `name_or_path` | str | **required** | App name (Start-Menu friendly name, incl. UWP) or executable path |
| `args` | list[str] | `None` | Trailing positional arguments passed to the app |

**Output**: `PID: N` for a Win32 process; `AppID: ...` (with `PID: 0`) for a UWP/Store app launched via its AppUserModelID.

### app switch

Switch to (bring to front) a window by title or process name.

```bash
agent-windows app switch "Notepad"
agent-windows app switch chrome
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `name_or_title` | str | **required** | Window title or process name |

**Output**: The switched-to window's title + handle, plus a foreground confirmation. `SetForegroundWindow`'s return is advisory (focus-stealing prevention can block the switch without erroring), so the command re-reads the real foreground window and reports it:

```
## App Switch
Window: "Notepad"
Handle: 197642
Foreground: yes                        # or: no (blocked by focus-stealing prevention)
```

Machine-readable sibling keys: `hwnd` (int) and `foreground_confirmed` (bool) — use the latter to detect a blocked switch and retry (e.g. click the target first to satisfy focus rules).

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

Run a PowerShell command. Output is forced to UTF-8 on the PowerShell side (so CJK and other non-ASCII output decodes cleanly), and is read incrementally; a command whose combined stdout+stderr exceeds `shell.max_output_bytes` (default 1 MiB) is TERMINATED at the cap with an `[output truncated: command terminated after reaching M bytes]` marker. This bounds daemon memory (a runaway like `1..1e6 | %{ Write-Output ("X"*1000) }` can't OOM the single-threaded daemon within the timeout) and frees the worker instead of running the runaway for the full timeout. `max_output_bytes = 0` is an explicit opt-out (no cap, no terminate).

```bash
agent-windows shell "Get-Process | Select-Object -First 5"
agent-windows shell "Get-Process" --timeout 60     # override default 30s
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `command` | str | **required** | PowerShell command |
| `--timeout` / `-t` | float | `0` (= config `shell.timeout`, 30) | Execution timeout in seconds |

**Output**: Command stdout/stderr with an `Exit code: N` line. A timeout raises error code `-1003` (WAIT_TIMEOUT). A command matching any pattern in `shell.blacklist` (default `[]` — empty, so **no commands are blocked by default**) is rejected before execution with `-1004` (PERMISSION_DENIED); every attempt — allowed or blocked — is still recorded in `shell_audit.log`. `shell.timeout`, `shell.max_output_bytes`, and `shell.blacklist` are configurable in `config.toml` (see `references/configuration.md`).

### fs read / fs write / fs list / fs copy / fs move / fs delete / fs search / fs info

File system operations. `read`/`write`/`list` cover the common cases; `copy`/`move`/`delete`/`search`/`info` handle the rest — prefer these over shelling out to `Copy-Item`/`Remove-Item` (they round-trip through the daemon in one call). `fs delete` sends to the **Recycle Bin** by default (reversible); use `--permanent` to force-delete or `--dry-run` to preview what would go.

```bash
agent-windows fs read C:\Users\test\file.txt
agent-windows fs read C:\Users\test\file.txt --offset 10 --limit 50   # line range
agent-windows fs write C:\Users\test\out.txt "Hello, World!"
agent-windows fs write C:\Users\test\log.txt "new line" --append
agent-windows fs list C:\Users
agent-windows fs list C:\Users --pattern *.py --recursive --show-hidden
agent-windows fs copy C:\a.txt C:\backup\a.txt --overwrite
agent-windows fs move C:\old.txt C:\new.txt
agent-windows fs delete C:\temp\junk --recursive          # → Recycle Bin (default, reversible)
agent-windows fs delete C:\temp\junk --permanent --recursive  # → gone for good
agent-windows fs delete C:\temp\junk --dry-run            # preview; nothing deleted
agent-windows fs search C:\Projects "*.py"
agent-windows fs info C:\Users\test\file.txt
```

| Command | Parameters |
|---------|-----------|
| `fs read` | `path` (str, **required**), `--offset` (int, default 0 — 1-based start line, 0 = whole file), `--limit` (int, default 0 — max lines, 0 = to EOF; capped at 100000), `--encoding` (str, default `""` = UTF-8 — override for legacy codepage files, e.g. `gbk`, `latin-1`, `cp1252`) |
| `fs write` | `path` (str, **required**), `content` (str, **required**), `--append` (bool, default `false` — append instead of overwriting) |
| `fs list` | `path` (str, **required**), `--pattern` (str, default `""` — name glob filter), `--recursive` (bool, default `false` — walk subdirectories), `--show-hidden` (bool, default `false` — include dotfiles) |
| `fs copy` | `path` (str, **required**), `destination` (str, **required**), `--overwrite` (bool, default `false` — replace existing destination) |
| `fs move` | `path` (str, **required**), `destination` (str, **required**), `--overwrite` (bool, default `false`) |
| `fs delete` | `path` (str, **required**), `--recursive` (bool, default `false` — required to delete non-empty directories), `--permanent` (bool, default `false` — force-delete, bypass Recycle Bin), `--dry-run` (bool, default `false` — preview without deleting) |
| `fs search` | `path` (str, **required**), `pattern` (str, **required** — glob, e.g. `*.py`), `--no-recursive` (bool, default `false` — search top level only) |
| `fs info` | `path` (str, **required**) — returns metadata (size, timestamps, attributes) |

> **`fs delete` output (post-op confirm)**: the path's disappearance is verified — `Verified: gone` on success, `Still present (unexpected)` if the literal path somehow survives. Sibling `verified.gone` (bool). Recycling is **best-effort**: `FOF_ALLOWUNDO` is only a hint, so a too-large file or an unavailable Recycle Bin makes the delete permanent instead — recoverability is not guaranteed, and the tool reports only that the path is gone, not whether it was recycled or permanently deleted. `--dry-run` returns a preview with no `Verified` line.

> **Memory bounds (read/list/search)**: to keep the worker from loading huge trees into RAM, results are capped — `fs read` returns at most ~5 MiB (a larger file is truncated, or use `--offset`/`--limit` to stream just the lines you need; a line range is also byte-bounded so a multi-GB file is never read whole), `fs list` at 5000 entries, `fs search` at 1000 matches. When truncated, the output ends with a `[Truncated: ...]` notice.

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
| `clipboard get` | `--keyboard` / `-k` (bool, optional — tri-state: omitted → fall back to config) |
| `clipboard set` | `text` (str, **required**), `--keyboard` / `-k` (bool, optional — tri-state: omitted → fall back to config) |

`--keyboard` is an **optional** flag: when **omitted**, the command forwards no `keyboard` param to the daemon and behavior falls back to config (`clipboard.method`, default `"system"` — PowerShell API); when **present**, it forces keyboard simulation (Ctrl+C/V). There is **no** `--no-keyboard` form. So the effective default depends on config, not a hardcoded value.

### process list / process kill

Process management.

```bash
agent-windows process list
agent-windows process list --sort-by cpu --limit 20
agent-windows process list --name chrome          # filter by name substring
agent-windows process kill 1234                   # By PID
agent-windows process kill notepad                # By name
agent-windows process kill notepad --force        # 强杀 immediately (default: graceful)
```

| Command | Parameters |
|---------|-----------|
| `process list` | `--sort-by` (str, default `memory` — `memory`/`cpu`/`name`, descending), `--limit` (int, default `0` = all — cap rows to top-N), `--name` (str, default `""` — case-insensitive substring filter) |
| `process kill` | `pid_or_name` (str/int, **required**), `--force` (bool, default `false` — `false` = graceful/WM_CLOSE lets the app save, `true` = immediate 强杀) |

> **`process list` output**: a markdown table — `PID | Name | Memory (MB) | CPU (total s) | Parent`. `CPU (total s)` is lifetime CPU seconds (not instant %). `Parent` is the parent PID (blank for protected/system procs). A `processes[]` sibling (daemon-direct clients; not via MCP/CLI, which return the table string) carries each row machine-readably with the full set — `pid`, `name`, `mem_mb`, `cpu_sec`, `parent_pid`, `cmdline`, `exe_path` — so two same-named procs (e.g. two `chrome.exe`) are told apart by `cmdline` without parsing markdown. `cmdline`/`exe_path`/`parent_pid` come from `Win32_Process` (CIM) and are `null` for protected/system processes (~50%); Mem/CPU/Name always come from `Get-Process`.

> **`process kill` output (post-op recheck)**: after taskkill reports success the tool rechecks liveness — `confirmed gone` (the PID no longer resolves / zero matching image names remain) or `still running (N remaining; retry with force=True)` when a graceful `WM_CLOSE` was ignored or the app prompted to save. Sibling `killed.confirmed_dead` (bool) and `killed.remaining` (int, name path). The recheck is **not** auto-retried with `/F` — decide yourself whether to escalate.

### notify

Send a Windows toast notification (title + message) via PowerShell WinRT `ToastNotificationManager`.

```bash
agent-windows notify "Build complete" "All tests passed"
agent-windows notify "Alert" "Disk 95% full" --app-id my.app
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `title` | str | **required** | Toast heading (max 256 chars) |
| `message` | str | **required** | Toast body text (max 1024 chars) |
| `--app-id` | str | PowerShell's AUMID | AppUserModelID to attribute the toast to |

**Output**: `sent` (bool), `exit_code`, `app_id`. Title/message are XML-escaped (safe for any content); `app_id` must not contain a single quote. A custom `--app-id` only displays reliably if a matching Start Menu shortcut is registered.

### registry get / set / delete / list

Windows Registry operations via PowerShell cmdlets. Paths use PowerShell drive format (`HKCU:\Software\MyApp`).

```bash
agent-windows registry list HKCU:\Software\MyApp
agent-windows registry get HKCU:\Software\MyApp Version
agent-windows registry set HKCU:\Software\MyApp Count 42 --type DWord
agent-windows registry delete HKCU:\Software\MyApp Version   # Delete a value
agent-windows registry delete HKCU:\Software\MyApp           # Delete the whole key
```

| Command | Parameters |
|---------|-----------|
| `registry get` | `path` (str, **required**), `name` (str, **required**) |
| `registry set` | `path` (str, **required**), `name` (str, **required**), `value` (**required**), `--type`/`-t` (str, default `String`) |
| `registry delete` | `path` (str, **required**), `name` (str, optional — omit to delete the whole key) |
| `registry list` | `path` (str, **required**) |

**`--type` values**: `String` (default), `ExpandString`, `DWord`, `QWord`, `MultiString` (value = JSON array, e.g. `'["a","b"]'`), `Binary` (value = hex string, e.g. `deadbeef`).

**Output**: `exit_code`, `stdout`, `stderr`. `get` also returns `value` in its **native type** (DWord/QWord→int, String/ExpandString→str, MultiString→list[str], Binary→list[int]) plus a `reg_type` sibling (PS enum name: `String`/`ExpandString`/`DWord`/`QWord`/`MultiString`/`Binary`) and annotates the output line `(type: <kind>)` — so a DWord `1` is distinguishable from a String `"1"`. `reg_type` is the PS enum name (not `REG_*`), symmetric with `set --type` (a get `DWord` round-trips to `set --type DWord`); Binary displays as hex, MultiString as `"; "`-joined. The `value`/`reg_type` siblings are daemon-direct-client only; MCP/CLI return the `output` string. Paths must start with `HKCU/HKLM/HKCR/HKU/HKCC/HKPD`. All interpolated strings are single-quote-escaped (safe for any content). A missing value/key surfaces as a non-zero `exit_code` with stderr (not an exception). On a successful `delete`, a `Remaining values:` (value delete) or `Remaining sub-keys:` (whole-key delete) line is appended along with a `remaining` sibling (list of surviving names; empty shows `(none)`) — so you know what's left without a second call.

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

### ping

Daemon health check (single IPC round-trip). Useful to confirm the daemon is reachable and measure round-trip latency — a fast way to distinguish "daemon down" from "command slow" when diagnosing hangs.

```bash
agent-windows ping
```

No parameters.

**Output**: `{status, message}` (`status: "ok"`, `message: "pong"`).

### config init / show / set / doctor

Configuration management. The launcher forwards these to the bundled Python CLI transparently, so use `agent-windows config ...` directly.

```bash
agent-windows config init              # Create default config file
agent-windows config show              # Print full config
agent-windows config set tree.max_depth 50   # Set a config value
agent-windows config doctor            # Check whether the vision model is configured
```

Config file: `~/.agent-windows/config.toml`

### mcp serve / auth / install / uninstall

MCP server. The launcher forwards these to the bundled Python CLI transparently, so use `agent-windows mcp ...` directly. Requires the `mcp` extra (`uv tool install agent-windows` includes it; from source run `uv sync --extra mcp`).

```bash
agent-windows mcp serve                                          # stdio (default; Claude Desktop / Cursor)
agent-windows mcp serve --transport streamable-http --port 8765  # remote clients
agent-windows mcp auth --transport streamable-http --host 0.0.0.0 --port 8000 --with-tls
agent-windows mcp install          # auto-start the HTTP server at login (Scheduled Task)
agent-windows mcp uninstall        # stop + remove the login auto-start task
```

`serve` starts the FastMCP server, which auto-starts the daemon on the first tool call — no separate `daemon start` needed. Defaults come from the `[mcp]` config section; explicit flags override. `auth_key` and TLS only apply to HTTP transports (sse / streamable-http), never stdio. Relative `ssl_certfile`/`ssl_keyfile` resolve against the config file's directory (see [TLS path resolution](configuration.md#tls-path-resolution)); a configured file that is missing fails fast before the server starts.

`auth` generates a bearer token (DPAPI-encrypted at rest in `[mcp] auth_key`), optionally a TLS cert, saves them to config, and prints the ready-to-paste MCP client config snippet (stdio / sse / streamable-http). It also auto-detects the Claude Desktop install (MSIX Store version's virtualized `%LOCALAPPDATA%\Packages\Claude_*\...\Claude` first, then standard `%APPDATA%\Claude`) and prints the real `claude_desktop_config.json` path; the stdio snippet uses the absolute `agent-windows.exe` path under MSIX (the sandbox doesn't inherit PATH). `--with-tls` writes the generated cert/key as **absolute** paths, so they're portable regardless of launch CWD. Regenerate with `--force`.

| Subcommand | Parameters |
|------------|------------|
| `mcp serve` | `--transport` (stdio\|sse\|streamable-http), `--host`, `--port`, `--debug`, `--tools`, `--exclude-tools`, `--ip-allowlist`, `--cors-origins`, `--allow-insecure-remote`, `--stateless-http`, `--oauth-client-id`, `--oauth-client-secret`, `--telemetry` (see [MCP.md](../../../MCP.md) for the full option reference) |
| `mcp auth` | `--transport`, `--host`, `--port`, `--with-tls`, `--force` |
| `mcp install` | `--transport`, `--host`, `--port`, `--force` |
| `mcp uninstall` | — |

`--tools`/`--exclude-tools` are enforced: removed tools cannot be invoked, including via `batch`. Each flag also has an `AGENT_WINDOWS_*` env-var equivalent (see the Global Options table below).

The server exposes ~57 tools mapping 1:1 to the daemon methods (screenshot, snapshot, observe, ground, detect, ask_vision, highlight, diff, find_image, pixel_at, get_text, element_state, cleanup_screenshots, list_displays, click, dblclick, hover, move, mouse_down, mouse_up, drag, scroll, type, shortcut, key_down, key_up, expand, collapse, app_launch/switch/resize/list, shell, fs_read/write/list/copy/move/delete/search/info, clipboard_get/set, process_list/kill, notify, registry, lock, sleep, shutdown, restart, batch, multi_select, multi_edit, ping, wait, wait_for). Same usage tips apply: call `snapshot` first for pixel-exact coordinates, prefer it over `ground`/`detect` for named controls, and always `app switch` before input.

---

## Power

### lock

Lock the workstation (equivalent to Win+L). Recoverable — log back in to resume.

```bash
agent-windows lock
```

No parameters.

**Output**: `{output}`

### sleep

Suspend the system to sleep (S3). Recoverable — wake to resume.

```bash
agent-windows sleep
```

No parameters.

**Output**: `{output}`

### shutdown

Shut down the system. Optionally delay the shutdown and/or force-close applications.

```bash
agent-windows shutdown                    # Immediate shutdown
agent-windows shutdown --delay 60        # 60-second delay
agent-windows shutdown --delay 60 --force
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `--delay` / `-d` | int | `0` | Delay before shutdown, in seconds (0–600, default 0 = immediate) |
| `--force` | bool | `false` | Force-close apps without waiting for save dialogs |

A scheduled shutdown (`--delay N` where `N > 0`) can be canceled by the user via `shutdown /a` or the Windows shutdown UI. `--force` skips the graceful shutdown sequence — apps lose unsaved data.

**Output**: `{output}`

### restart

Restart the system. Same parameters as `shutdown`.

```bash
agent-windows restart                     # Immediate restart
agent-windows restart --delay 120        # 2-minute delay
agent-windows restart --delay 120 --force
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `--delay` / `-d` | int | `0` | Delay before restart, in seconds (0–600, default 0 = immediate) |
| `--force` | bool | `false` | Force-close apps without waiting for save dialogs |

**Output**: `{output}`

---

## Global Options

Available on all commands:

| Flag | Description |
|------|-------------|
| `--no-daemon` | Direct execution (bypass daemon, for debugging) |
| `--format text\|json` | Output format |
| `--debug` | Verbose logging. Sets the `AGENT_WINDOWS_DEBUG` env var (`enable_debug()`); the auto-started daemon inherits it, and any module reads it via `is_debug()` instead of receiving a flag. Also settable directly as `AGENT_WINDOWS_DEBUG=1`. |
| `--config <path>` | Alternate config.toml (default `~/.agent-windows/config.toml`). Propagated to the daemon; reads *and* writes (`config set`/`mcp auth`) target this file. State files (pid/lock/logs) stay in the default location. Missing file = defaults. |
