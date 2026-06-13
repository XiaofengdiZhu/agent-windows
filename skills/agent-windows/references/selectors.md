---
name: selectors
description: "Deep dive on the @eN element selector system, snapshot output format, coordinate extraction, and ref lifecycle in agent-windows. Read when you need to understand how to go from snapshot output to action coordinates."
allowed-tools: Bash(agent-windows:*)
---

# Selectors and Coordinates

## How @eN Refs Work

`@eN` is a **1-based index** into the `interactive_elements` array returned by `snapshot` or `observe`. When you call `agent-windows snapshot`, the daemon:

1. Traverses the UIA accessibility tree via COM.
2. Collects all interactive elements (controls that are both `is_interactive` and `is_enabled`).
3. Renders them into a text snapshot with an **Interactive Elements table**.
4. Assigns `@e1`, `@e2`, `@e3`, ... in order of discovery.

The index is positional — `@e3` always means "the third interactive element in the most recent snapshot". If the UI changes between snapshots, the same `@e3` may refer to a completely different element.

## Snapshot Output Format

A typical `observe` or `snapshot` output has four sections:

```
## Desktop State
Cursor: (960, 540)
Resolution: 1920x1080
Displays: 1
Active Window: "Untitled - Notepad" (NOTEPAD)

## Windows
| # | Title | App | Status | Position |
|---|-------|-----|--------|----------|
| 1 | Untitled - Notepad | NOTEPAD | active | (100,100,900,700) |

## Accessibility Tree
Window "Untitled - Notepad" (NOTEPAD) [active]
  ├─ Edit "" (100,150,900,680) [focused] [interactive]
  ├─ MenuItem "File" (0,0,37,32) [interactive]
  ├─ MenuItem "Edit" (37,0,74,32) [interactive]

## Interactive Elements
| Ref | Type     | Name | Position          | State   |
|-----|----------|------|-------------------|---------|
| @e1 | Edit     |      | (100,150,900,680) | focused |
| @e2 | MenuItem | File | (0,0,37,32)       |         |
| @e3 | MenuItem | Edit | (37,0,74,32)      |         |
```

### Section Breakdown

**Desktop State**: Current cursor position, screen resolution, number of displays, and the active (foreground) window.

**Windows Table**: All visible windows with their titles, process names, active status, and bounding rectangles.

**Accessibility Tree**: Hierarchical view of UIA elements. Each line shows:
- Control type (with "Control" suffix stripped: `ButtonControl` → `Button`)
- Name in quotes (may be empty)
- Bounding box in `(left, top, right, bottom)` format
- State flags: `[focused]`, `[disabled]`, `[interactive]`

**Interactive Elements Table**: The primary targeting reference. Each row has:
- `@eN` ref for addressing
- Control type
- Element name (truncated to 40 chars)
- Position as `(left, top, right, bottom)` — **these are pixel coordinates**
- State flags (focused, disabled)

## From @eN to Coordinates

Most input commands (`click`, `type`, `scroll`, etc.) require **pixel coordinates**, not `@eN` refs. To act on element `@e3`:

1. Find `@e3` in the Interactive Elements table.
2. Read its Position: `(left, top, right, bottom)`.
3. Compute the center: `cx = (left + right) // 2`, `cy = (top + bottom) // 2`.
4. Pass to the command: `agent-windows click cx cy`.

**Example**:

```
| @e5 | Button | Submit | (400,350,500,385) |         |
```

Center: `cx = (400+500)//2 = 450`, `cy = (350+385)//2 = 367`.

```bash
agent-windows click 450 367
```

## Commands That Accept @eN Directly

Some commands resolve the selector to coordinates internally:

| Command | Selector Usage |
|---------|---------------|
| `get-text @e3` | Returns the element's `name` property |
| `is visible @e3` | Checks `CurrentIsOffscreen` |
| `is enabled @e3` | Checks `CurrentIsEnabled` |
| `wait @e3` | Polls until the element is found in the tree |
| `screenshot -s @e3` | Crops screenshot to element's bounding box |

For all other commands (`click`, `type`, `scroll`, `drag`, `hover`, etc.), extract coordinates from the Position column.

## Coordinate Space

- All coordinates are **physical screen pixels**.
- DPI awareness is handled by the daemon (per-monitor DPI V2).
- No conversion is needed by the caller — coordinates from the snapshot map 1:1 to physical pixels.
- Multi-monitor: coordinates span the virtual screen. Use `--monitor` on `screenshot` to target a specific display.

## Ref Lifecycle Rules

Refs are **ephemeral** — they are only valid against the snapshot that generated them.

### When Refs Go Stale

Refs become invalid after any action that may change the UI:

- **Clicking** a button (may open dialog, navigate, toggle state)
- **Typing** in a search field (may filter a list, show suggestions)
- **Keyboard shortcuts** (may switch views, open menus)
- **App switching** (completely different window)
- **Time passing** (dynamic content, animations, notifications)

### The Re-Snapshot Rule

**Always re-snapshot (or re-observe) before the next interaction.** Never reuse refs from a previous observation — the UI may have changed completely between calls.

Correct pattern:

```bash
agent-windows observe          # Get refs
agent-windows click 450 320    # Click button
agent-windows observe          # Re-snapshot (refs are now fresh)
agent-windows type "hello"     # Use new refs for next action
```

Incorrect pattern:

```bash
agent-windows observe          # Get refs
agent-windows click 450 320    # Click button
# UI changed! Old refs are stale!
agent-windows click 600 400    # Using stale coordinates — may miss
```

## Best Practices

1. **Always snapshot before interacting.** Even if you just acted and "know" where things are, the UI may have shifted.

2. **Re-snapshot after every action that changes the UI.** Click, type, shortcut — assume the worst.

3. **Use `observe` for combined view.** It gives you both the UIA tree and a screenshot, which helps verify that the snapshot matches what you expect.

4. **Cross-reference with vision when UIA is incomplete.** Some controls (custom-drawn, Chromium web content, games) don't appear in the UIA tree. Use `ground` or `detect` for those.

5. **Check the State column.** Elements marked `[disabled]` won't respond to interaction. Elements marked `[focused]` already have keyboard focus — you can `type` directly without clicking first.

6. **Bounding box (0,0,0,0) or empty.** Some elements have no bounding box. These are typically logical containers or offscreen elements. Fall back to vision tools for these.
