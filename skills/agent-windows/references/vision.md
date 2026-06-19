<!-- Reference doc — loaded on demand via the table in SKILL.md, not discovered as a standalone skill. -->

# Visual Tools Reference

agent-windows has three vision-powered commands for cases where the UIA accessibility tree cannot see an element (custom-drawn UI, canvas, Chromium web page content, games).

## Three Commands Compared

| Feature | `ground` | `detect` | `ask-vision` |
|---------|----------|----------|--------------|
| **Purpose** | Locate a single element | Find multiple targets | Free-form visual Q&A |
| **Returns** | One `(x, y)` point | List of bounding boxes | Text response |
| **Precision** | ±~100 px (two-pass) | ±~100 px (single-pass) | Descriptive only |
| **Best for** | Clicking a specific button | Finding all icons of a type | Understanding screen state |
| **Passes** | Coarse → fine | Single pass | Single pass |

**Rule**: Use `snapshot` first. Only reach for vision tools when the UIA tree doesn't contain the target.

## ground — Single Element Location

Locates one element by natural-language description using a two-pass coarse-to-fine strategy.

```bash
agent-windows ground "the Submit button"
agent-windows ground "打印图标"
agent-windows ground "the search box in the top-right corner"
```

**How it works**:
1. Captures a screenshot.
2. **Coarse pass**: Sends the full screenshot to the vision model with the description. Model returns approximate coordinates.
3. **Fine pass**: Crops a region around the coarse result, sends the crop for refinement.
4. Returns the refined `(x, y)` coordinate with a confidence score.

**Output**: `{output, x, y, confidence, method, screenshot_path}`

| Field | Type | Description |
|-------|------|-------------|
| `x` | int | X coordinate (physical pixels) |
| `y` | int | Y coordinate (physical pixels) |
| `confidence` | str | `"high"`, `"medium"`, or `"low"` |
| `method` | str | `"visual-two-pass"`, `"visual-two-pass-drift"`, or `"visual-coarse-only"` |
| `screenshot_path` | str | Path to the annotated screenshot |

**Usage with click**:

```bash
# Get coordinates, then click
agent-windows ground "the print icon"
# From output: x=350, y=42
agent-windows click 350 42
```

## detect — Multiple Target Detection

Finds all instances of a described element, returning bounding boxes for each.

```bash
agent-windows detect "PDF file icons on the desktop"
agent-windows detect "all red error messages"
agent-windows detect "checkboxes" --max-targets 10
```

**How it works**:
1. Captures a screenshot (or uses provided image).
2. Sends screenshot to vision model with description and `max_targets` limit.
3. Model returns a list of bounding boxes.
4. Optionally saves an annotated screenshot with boxes drawn.

**Output**: `{output, description, count, targets, screenshot_path}`

Each target in `targets`:

| Field | Type | Description |
|-------|------|-------------|
| `index` | int | 1-based index |
| `label` | str | Description label |
| `box` | dict | `{x1, y1, x2, y2}` bounding box |

**Computing click center from a box**:

```
box = {x1: 100, y1: 200, x2: 150, y2: 250}
cx = (100 + 150) // 2 = 125
cy = (200 + 250) // 2 = 225
agent-windows click 125 225
```

## ask-vision — Free-Form Visual Q&A

Sends a screenshot with a custom prompt to the vision model. Returns the raw text response. Does **not** return coordinates.

```bash
agent-windows ask-vision "What windows are currently open?"
agent-windows ask-vision "Is there an error dialog visible?"
agent-windows ask-vision "Describe the toolbar icons in this application"
agent-windows ask-vision "What text is shown in the status bar?" -i screenshot.png
```

**Use cases**:
- Understanding icon meanings (icon-only toolbars)
- Reading on-screen text (OCR)
- Checking visual state (is a progress bar full? is there a green checkmark?)
- Describing what the model sees before deciding how to act

**Output**: `{output, response, screenshot_path, model_used}`

| Field | Type | Description |
|-------|------|-------------|
| `response` | str | Vision model's text response |
| `screenshot_path` | str | Path to the screenshot used |
| `model_used` | str | Model identifier (e.g. `gpt-4o-mini`) |

## Vision Model Configuration

Vision tools are powered by the `[grounding]` config section.

```toml
[grounding]
enabled = true
engine = "openai"           # openai / anthropic / gemini
model = "gpt-4o-mini"
api_base = "https://api.openai.com/v1"
api_key = ""                # or AGENT_WINDOWS_GROUNDING_API_KEY env var
timeout = 30
image_format = "JPEG"       # JPEG or PNG
jpeg_quality = 85           # JPEG quality (1-95), ignored when PNG
```

### Supported Engines

| Engine | `api_base` default | Notes |
|--------|-------------------|-------|
| `openai` | `https://api.openai.com/v1` | GPT-4o, GPT-4o-mini |
| `anthropic` | `https://api.anthropic.com` | Claude models |
| `gemini` | Google AI API | Gemini Pro Vision |

### Setting the API Key

Three ways (priority order: env var > config > interactive):

```bash
# Environment variable (recommended for security)
export AGENT_WINDOWS_GROUNDING_API_KEY="sk-..."

# Config file
agent-windows config set grounding.api_key "sk-..."

# Or leave empty and set at runtime
```

### Overriding the Model

```bash
agent-windows ask-vision "Describe this" -m gpt-4o
agent-windows detect "buttons" -m claude-sonnet-4-20250514
```

## Mixed UIA + Vision Strategy

The recommended approach for complex UIs (icon-only toolbars, custom controls, mixed content):

### Step 1: Start with UIA

```bash
agent-windows observe                    # Get UIA tree + screenshot
```

Read the Interactive Elements table. If your target is there with coordinates, use those — they're pixel-exact.

### Step 2: Use vision for gaps

If the target isn't in the UIA tree (custom-drawn, canvas, browser content):

```bash
agent-windows ask-vision "Describe the toolbar icons"    # Understand what's there
agent-windows ground "the print icon"                     # Get coordinates
```

### Step 3: Cross-reference

Use both sources together:
- UIA tree tells you the layout structure and named elements
- Vision fills in the gaps for unnamed or custom-drawn elements
- Cross-reference positions to avoid ambiguity

### Example: Navigating an icon-only toolbar

```bash
agent-windows observe                    # UIA shows menu items but not toolbar icons
agent-windows ask-vision "What are the icons in the toolbar at the top?"
# Model: "From left: New, Open, Save, Print, Undo, Redo..."
agent-windows ground "the Print icon"    # Get coordinates for Print
agent-windows click 350 42               # Click using vision coordinates
agent-windows observe                    # Re-snapshot to see what changed
```

## Precision and Limitations

- **Vision coordinates have ~±100 px drift**, typically biased toward the left edge of text. This is acceptable for large buttons and icons but may miss small targets.
- **Always verify after clicking.** After using vision-guided coordinates, `observe` to confirm the correct element was activated.
- **`ground` is more precise than `detect`** because it uses two passes. For critical single-target clicks, prefer `ground`. Check `confidence`: only trust `"high"` results for precise targeting; `"low"` may be off by more than 100 px.
- **Vision models may hallucinate.** If `ground` returns coordinates that seem wrong (e.g., outside the visible area), re-observe and try again with a more specific description.
- **Browser web content is invisible to UIA.** Chromium browsers (Edge, Chrome) only expose browser chrome (address bar, tabs, toolbar) via UIA. For page content, vision tools are your only option.
