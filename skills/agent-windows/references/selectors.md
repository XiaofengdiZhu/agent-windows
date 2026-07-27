<!-- Reference doc — loaded on demand via the table in SKILL.md, not discovered as a standalone skill. -->

# Selectors and Coordinates

## How @eN Refs Work

`@eN` references an element in the Interactive Elements table returned by `snapshot` or `observe`. Refs are assigned in discovery order: `@e1`, `@e2`, `@e3`, ...

The index is positional — `@e3` always means "the third interactive element in the most recent snapshot". If the UI changes between snapshots, the same `@e3` may refer to a completely different element.

## Snapshot Output Format

A typical `observe` or `snapshot` output has four sections. Below is a real `observe` run (File Explorer on Windows) — note the non-round coordinates, Chinese names, and multi-window layout you'll commonly see in practice:

```
## Desktop State
Cursor: (2305, 1188)
Resolution: 3072x1920
Displays: 1
Active Window: "ZCode" (ZCode.exe)

## Windows
| # | Title | App | Status | Position |
|---|-------|-----|--------|----------|
| 1 | ZCode | ZCode.exe | active | (336,112,2736,1712) |
| 2 | references - 文件资源管理器 | explorer.exe |  | (0,0,3072,1824) |
| 3 | 设置 | SystemSettings.exe |  | (0,13,3072,1837) |

## Accessibility Tree
Desktop
  └─ Window "ZCode" (ZCode.exe) [active]
    ├─ Button "最小化" (2460,114,2550,208) [interactive]
    ├─ Button "最大化" (2550,114,2642,208) [interactive]
    ├─ Button "关闭" (2642,114,2734,208) [interactive]
    └─ Document "ZCode" (336,112,2736,1712) [interactive]
  └─ Window "references - 文件资源管理器" (explorer.exe)
    ├─ Button "共享" (2503,848,2647,906) [interactive]
    ├─ Button "属性" (2503,1228,2647,1286) [interactive]
    └─ ListItem "selectors.md" (361,450,1711,498) [interactive]

## Interactive Elements
| Ref | Type | Action | Name | Window | Position | State |
|-----|------|--------|------|--------|----------|-------|
| @e1 | Button | click | 最小化 | ZCode | (2460,114,2550,208) |  |
| @e2 | Button | click | 最大化 | ZCode | (2550,114,2642,208) |  |
| @e3 | Button | click | 关闭 | ZCode | (2642,114,2734,208) |  |
| @e4 | Document | scroll | ZCode | ZCode | (336,112,2736,1712) |  |
| @e5 | Button | click | 共享 | 文件资源管理器 | (2503,848,2647,906) |  |
| @e6 | Button | click | 属性 | 文件资源管理器 | (2503,1228,2647,1286) |  |
```

### Section Breakdown

**Desktop State**: Current cursor position, screen resolution, number of displays, and the active (foreground) window.

**Windows Table**: All visible windows with their titles, process names, active status, and bounding rectangles.

**Accessibility Tree**: Hierarchical view of UIA elements. Each line shows:
- Control type (with "Control" suffix stripped: `ButtonControl` → `Button`)
- Name in quotes (may be empty)
- Bounding box in `(left, top, right, bottom)` format
- State flags: `[focused]`, `[disabled]`, `[interactive]`

Empty structural containers (unnamed `Pane`/`Group`/`ToolBar`/`Tab`/`MenuBar`/`Custom` with no useful descendants) are pruned to reduce noise — only containers that group an interactive element or carry a name appear. This affects only the tree text, not the Interactive Elements table (`@eN` refs are unaffected).

**Interactive Elements Table**: The primary targeting reference. Each row has:
- `@eN` ref for addressing
- Control type
- Action — the verb(s) the element supports (`click`/`fill`/`toggle`/`select`/`slide`/`scroll`/`expand`; multiple joined with `/`). Tells you what to *do* before you act.
- Element name (truncated to 40 chars)
- Position as `(left, top, right, bottom)` — **these are pixel coordinates**
- State flags (focused, disabled) plus richer attributes (toggle_state, value, scrollable…)

## From @eN to Coordinates

**Preferred:** pass the `@eN` ref directly to input commands via `--selector`/`-s`. The command resolves the element's bbox center internally — no manual math:

```bash
agent-windows click -s @e5
agent-windows type "text" --at-selector @e3
agent-windows scroll -s @e3 --direction up
```

`--selector` works on `click`, `dblclick`, `hover`, `move`, `mouse-down`, `mouse-up`, `scroll`, and `type` (`--at-selector`). It overrides any explicit coordinates when both are given.

**Fallback** (manual, when you need a non-center point): extract coordinates yourself.

To act on element `@e3` at a specific point:

1. Find `@e3` in the Interactive Elements table.
2. Read its Position: `(left, top, right, bottom)`.
3. Compute the target point, e.g. center: `cx = (left + right) // 2`, `cy = (top + bottom) // 2`.
4. Pass to the command: `agent-windows click cx cy`.

**Example** (using the real snapshot above):

```
| @e5 | Button | click | 共享 | (2503,848,2647,906) |  |
```

Center: `cx = (2503+2647)//2 = 2575`, `cy = (848+906)//2 = 877`.

```bash
agent-windows click 2575 877      # explicit coords
agent-windows click -s @e5        # equivalent, via selector
```

## Commands That Accept @eN Directly

Commands that resolve a selector to coordinates or use it as a target:

| Command | Selector Usage |
|---------|---------------|
| `get-text @e3` | Returns the element's name/text |
| `is visible @e3` | Checks if the element is on-screen |
| `is enabled @e3` | Checks if the element is enabled |
| `wait @e3` | Polls until the element is found in the tree |
| `screenshot -s @e3` | Crops screenshot to element's bounding box |
| `click -s @e3` | Clicks the element's bbox center |
| `dblclick -s @e3` | Double-click the element center |
| `hover -s @e3` | Hover the element center |
| `move -s @e3` | Move to the element center |
| `mouse-down -s @e3` / `mouse-up -s @e3` | Press/release at element center |
| `scroll -s @e3` | Scroll at the element center |
| `type --at-selector @e3` | Click the element center before typing |

For `drag` and `shortcut`, extract coordinates/keys manually as before.

## Coordinate Space

- All coordinates are **physical screen pixels**.
- No DPI conversion is needed — coordinates from the snapshot map 1:1 to physical pixels (per-monitor DPI is handled automatically).
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
