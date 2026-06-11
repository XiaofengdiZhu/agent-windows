# agent-windows 设计文档

## 概述

agent-windows 是一款 Windows 系统 Computer-Use CLI 工具，面向 AI Agent（Claude Code、Open Claw 等），也支持人类直接使用。提供桌面截图、UIA 无障碍树遍历、视觉模型坐标定位、鼠标键盘自动化等能力。

### 核心定位

- CLI 优先，AI Agent 通过子进程调用
- 三种观察模式：UIA Snapshot（无障碍树）、Screenshot（截图）、Grounding（视觉坐标定位）
- 可配置多模型视觉定位（OpenAI 兼容 API、Anthropic、Gemini 等）

### 为什么全新构建而非扩展 Windows-MCP

| 因素 | 扩展 Windows-MCP | 全新构建 |
|------|------------------|----------|
| 架构 | MCP Server 优先，CLI 仅用于启动 | CLI 优先，自然适配 Agent 调用 |
| 输出格式 | MCP 协议 JSON | 面向 AI 消费的结构化文本 |
| 可扩展性 | 受上游架构约束 | 自由设计命令体系 |
| 视觉定位 | 补丁式添加 | 原生集成 |
| 维护 | 依赖上游合并 | 独立演进 |

## 架构

### Client-Daemon 模式

解决 Python 启动慢问题（comtypes、Pillow 等重导入约 200-500ms）。借鉴 agent-browser 的 Daemon 模式：

- **Daemon（常驻进程）：** 持有所有重导入（comtypes、uia、Pillow、dxcam），通过 Windows Named Pipe 接收命令
- **CLI（轻量客户端）：** 仅 import typer + json + socket（~5-10ms），发送 IPC 消息到 Daemon
- **首次调用** 自动启动 Daemon，后续调用走 IPC（~10-20ms）
- **空闲超时** 自动退出 Daemon（默认 30 分钟）
- **`--no-daemon`** 降级模式用于调试，跳过 Daemon 直接执行

```
┌──────────────────────┐     Named Pipe      ┌──────────────────────┐
│   CLI (轻量入口)      │ ══════════════════→ │   Daemon (常驻进程)   │
│                      │ ←══════════════════ │                      │
│   仅解析参数          │     JSON-RPC        │   持有所有重导入:      │
│   发 IPC 消息         │                     │   comtypes, uia,     │
│   ~5-10ms 启动       │                     │   Pillow, dxcam...   │
│                      │                     │                      │
│                      │                     │   命令路由与执行       │
│                      │                     │   ┌──────────────┐   │
│                      │                     │   │  观察模块     │   │
│                      │                     │   │  Snapshot     │   │
│                      │                     │   │  Screenshot   │   │
│                      │                     │   │  Grounding    │   │
│                      │                     │   ├──────────────┤   │
│                      │                     │   │  输入模块     │   │
│                      │                     │   │  鼠标/键盘    │   │
│                      │                     │   ├──────────────┤   │
│                      │                     │   │  系统模块     │   │
│                      │                     │   │  App/Shell/FS │   │
│                      │                     │   └──────────────┘   │
│                      │                     │         │            │
│                      │                     │   ┌──────────────┐   │
│                      │                     │   │ UIA / Win32  │   │
│                      │                     │   │ (vendored)   │   │
│                      │                     │   └──────────────┘   │
└──────────────────────┘                     └──────────────────────┘
```

### Daemon 线程模型

COM STA（单线程单元）要求初始化 COM 的线程必须有 Windows 消息泵（`GetMessage`/`DispatchMessage`）。Named Pipe 阻塞 I/O 和 COM 调用不能在同一线程。

**Daemon 使用三线程模型：**

```
┌─ Daemon 进程 ─────────────────────────────────────┐
│                                                    │
│  ┌─ I/O 线程 (主线程) ──────────────────────────┐  │
│  │  监听 Named Pipe，接收 JSON-RPC 请求          │  │
│  │  将命令放入队列，等待结果，返回响应             │  │
│  └──────────────────────────────────────────────┘  │
│         │ ↑                                        │
│    放入队列 │ 返回结果                               │
│         ↓ │                                        │
│  ┌─ COM STA 工作线程 ───────────────────────────┐  │
│  │  pythoncom.CoInitialize() + 消息泵           │  │
│  │  从队列取命令 → 执行 UIA/鼠标/键盘操作        │  │
│  │  所有 COM/UIA 调用在此线程串行执行             │  │
│  └──────────────────────────────────────────────┘  │
│                                                    │
│  ┌─ 空闲监控线程 ──────────────────────────────┐  │
│  │  定时检查最后命令时间，超时则通知 Daemon 退出  │  │
│  └──────────────────────────────────────────────┘  │
└────────────────────────────────────────────────────┘
```

**并发模型：命令串行执行。** COM 对象具有线程亲和性，UIA 操作必须在同一 STA 线程。多个 CLI 客户端同时发命令时，I/O 线程将它们排入队列，STA 线程逐个处理。

**DPI 处理：** `SetProcessDpiAwareness(PerMonitorDpiAware)` 是进程级调用，只能调用一次，后续调用返回 `E_ACCESSDENIED`。Daemon 进程在主线程启动阶段（线程创建前）显式调用一次。vendored `uia/core.py` 中的模块级 DPI 调用需移除，改为 Daemon 统一管理。所有坐标（UIA、截图、鼠标）统一使用物理像素。

**Vendored UIA 初始化约束：** vendored `uia/core.py` 的 `_AutomationClient.__init__` 会调用 `CoInitialize(None)` 初始化 COM。Daemon 必须确保 `_AutomationClient.instance()` 首次调用发生在 COM STA 工作线程上（不在 I/O 线程）。实现方式：STA 工作线程启动时主动触发一次 UIA 初始化（如获取根元素），后续所有 UIA 操作自然在同一 STA 线程执行。

### IPC 协议

- **传输：** Windows Named Pipe (`\\.\pipe\agent-windows`)。Named Pipe 使用默认 ACL（仅当前用户可访问），不做额外安全控制。与 Windows-MCP 保持一致：工具提供完整系统访问能力，安全性由调用方（AI Agent）负责。
- **协议：** JSON-RPC 2.0，一行请求一行响应（newline-delimited）
- **请求示例：** `{"jsonrpc":"2.0","id":1,"method":"click","params":{"x":500,"y":300}}`
- **响应示例：** `{"jsonrpc":"2.0","id":1,"result":{"status":"ok"}}`
- **错误响应：** `{"jsonrpc":"2.0","id":1,"error":{"code":-32602,"message":"..."}}`

**错误码定义：**

| 错误码 | 含义 |
|--------|------|
| -32700 | JSON 解析错误 |
| -32600 | 无效请求 |
| -32601 | 方法未找到 |
| -32602 | 参数无效 |
| -1001 | 元素未找到 |
| -1002 | 坐标超出屏幕范围 |
| -1003 | 等待 UI 状态超时 |
| -1004 | 权限不足（UAC） |
| -1005 | 视觉模型 API 调用失败或超时 |
| -1999 | Daemon 内部错误（未分类） |

### Daemon 生命周期

**启动流程：**
1. CLI 发命令 → 检测 Named Pipe 是否存在
2. 不存在 → 启动 Daemon 进程（`python -m agent_windows --daemon`）
3. Daemon 初始化（导入重模块、启动 COM STA 工作线程、设置 DPI 感知）→ 创建 PID 文件 → 监听 Named Pipe
4. CLI 轮询连接（最多 25 次，间隔 200ms，总计 5 秒）。每次重试前检查 Daemon 进程是否已退出（快速失败而非等待超时）
5. 连接成功 → 发送命令 → 接收响应 → 退出 CLI

**运行时：**
- PID 文件：`%USERPROFILE%\.agent-windows\daemon.pid`（写入进程 PID + 启动时间）
- 版本文件：`%USERPROFILE%\.agent-windows\daemon.version`（写入工具版本号，用于升级后检测旧 Daemon）
- 空闲超时：30 分钟无命令后自动退出
- 日志：`%USERPROFILE%\.agent-windows\logs\daemon.log`，支持 `--debug` 开启详细日志

**崩溃恢复：**
- CLI 启动时读取 PID 文件，通过 `OpenProcess` 检查进程是否存活
- 进程已死 → 清理 PID 文件和 Named Pipe → 重新启动 Daemon
- 版本不匹配（工具已升级但旧 Daemon 运行中）→ 自动重启 Daemon

**关闭流程：**
- 空闲超时或 `daemon stop` 命令 → 通知 STA 线程停止 → 等待当前命令完成（最多 10 秒，超时则强制终止）→ 清理 PID 文件 → 退出

## 命令设计

### 观察命令

| 命令 | 说明 |
|------|------|
| `observe [--snapshot] [--screenshot] [--ground <desc>]` | 综合观察命令，可组合多种模式 |
| `snapshot` | UIA 无障碍树遍历，返回语义化文本 |
| `screenshot [--output PATH]` | 截图返回图片路径 |
| `ground <description>` | 视觉模型定位元素坐标 |
| `ask-vision <prompt> [--image PATH] [--model MODEL]` | 自由视觉问答：将截图（或指定图片）+ 自定义提示词发给视觉模型，返回模型的原始文本响应 |

#### `ask-vision` 详细设计

**用途：** 让 AI agent 利用视觉模型的理解能力进行任意视觉推理，不仅限于坐标定位。

**参数：**

| 参数 | 说明 |
|------|------|
| `<prompt>` | 自定义提示词，完全由调用方控制 |
| `--image PATH` | 指定图片文件路径（默认：自动截取当前屏幕） |
| `--model MODEL` | 覆盖配置文件中的视觉模型（可选） |
| `--output PATH` | 保存使用的截图（可选，便于调试） |

**示例：**

```bash
# 让视觉模型描述屏幕内容
agent-windows ask-vision "描述当前屏幕上有哪些窗口和它们的状态"

# 分析指定截图中的表格数据
agent-windows ask-vision "提取这张图中表格的所有数据，以 JSON 格式返回" --image step3.png

# 判断某个 UI 元素是否存在
agent-windows ask-vision "屏幕上是否有弹出的错误对话框？如果有，描述其内容"

# 读取图标状态
agent-windows ask-vision "任务栏上的网络图标是否显示已断开连接？"

# 结合 ground 使用：先用 ask-vision 分析，再用 ground 定位
agent-windows ask-vision "当前活动窗口的标题是什么？窗口中是否有未保存的文档标志（标题栏上的星号）？"
```

**与 `ground` 的区别：**

| | `ground` | `ask-vision` |
|---|---|---|
| 目的 | 精确定位元素的像素坐标 | 任意视觉问答 |
| 输出 | `{x, y, confidence}` 坐标 | 模型的原始文本响应 |
| 提示词 | 固定模板（可配），自动注入坐标解析指令 | 完全自定义 |
| 两轮裁剪 | ✅ 粗定位 → 精定位 | ❌ 单次调用 |
| 典型场景 | "点这个按钮" → 需要坐标 | "这个复选框是否勾选了？" → 需要语义判断 |

**实现要点：**
- 复用 `grounding` 的视觉模型 API 客户端和配置（engine / api_base / api_key / timeout）
- 不进行坐标解析，直接返回模型响应文本
- 如果 agent 需要坐标，使用 `ground`；如果需要语义理解、OCR、状态判断等，使用 `ask-vision`

### 输入命令

| 命令 | 说明 |
|------|------|
| `click <x> <y> [--right] [--double]` | 鼠标点击 |
| `type <text> [--at <x,y>] [--enter] [--clear]` | 输入文字。始终使用 Unicode 直接输入模式（绕过 IME），确保中英文可靠输入。`--clear` 先全选删除再输入 |
| `scroll <x> <y> [--direction up/down] [--amount N]` | 滚动。移动鼠标到 (x,y)，发送滚轮事件。若该位置有 UIA ScrollPattern 元素，优先使用 ScrollPattern |
| `move <x> <y>` | 移动鼠标 |
| `drag <x1> <y1> <x2> <y2> [--right]` | 拖拽：从 (x1,y1) 按下，拖动到 (x2,y2) 释放 |
| `shortcut <keys>` | 快捷键 (例: `ctrl+c`) |

### 系统命令

| 命令 | 说明 |
|------|------|
| `app launch <name_or_path> [--args ...]` | 启动应用。`<name_or_path>` 支持可执行文件名（如 `notepad`）、完整路径（如 `C:\Program Files\...\app.exe`）、或 Windows 开始菜单注册名 |
| `app switch <name_or_title>` | 切换到指定应用窗口。通过窗口标题模糊匹配或进程名匹配。使用 `SetForegroundWindow` 激活，处理最小化恢复 |
| `app resize <name_or_title> --width W --height H [--x X --y Y]` | 调整窗口大小和位置。使用 `SetWindowPos` |
| `app list` | 列出所有可见窗口 |
| `shell <command>` | PowerShell 命令 |
| `fs <read/write/list> <path>` | 文件系统 |
| `clipboard [get/set]` | 剪贴板 |
| `process [list/kill]` | 进程管理 |

### Daemon 命令

| 命令 | 说明 |
|------|------|
| `daemon start` | 启动 Daemon 进程（预加热用，正常使用时自动启动） |
| `daemon stop` | 停止 Daemon 进程 |
| `daemon status` | 查询 Daemon 状态 |

### 辅助命令

| 命令 | 说明 |
|------|------|
| `wait <seconds>` | 等待 |
| `config <init/show/set>` | 配置管理 |

**全局选项：** `--no-daemon` 跳过 Daemon，直接执行（用于调试）；`--format text|json` 切换输出格式；`--debug` 开启详细日志

## 输出格式

所有输出面向 AI Agent 消费，使用结构化文本（非 JSON）。支持 `--format text|json` 全局选项切换输出格式。

### snapshot 输出

```
## Desktop State
Cursor: (960, 540)
Resolution: 1920x1080
Displays: 2 (primary: 1)
Active Window: "记事本" (notepad.exe)

## Windows
| # | Title | App | Status | Position |
|---|-------|-----|--------|----------|
| 1 | 记事本 | notepad.exe | active | (100,100,800,600) |
| 2 | Chrome | chrome.exe | | (0,0,1920,1080) |

## Accessibility Tree
Desktop
  └─ Window "记事本" [active]
      ├─ MenuBar
      │   ├─ MenuItem "文件"
      │   └─ MenuItem "编辑"
      ├─ Edit "" [editable, focused]
      └─ StatusBar "第 1 行，第 1 列"

## Interactive Elements
| Ref | Type | Name | Position | State |
|-----|------|------|----------|-------|
| @e1 | MenuItem | 文件 | (110,30,150,50) | |
| @e2 | MenuItem | 编辑 | (160,30,200,50) | |
| @e3 | Edit | | (110,70,790,570) | focused |

## Screenshot
Path: C:\Users\...\screenshot.png
Size: 1920x1080
```

### ground 输出

```
## Grounding Result
Target: "保存按钮"
Coordinate: (520, 340)
Confidence: high
Method: visual-two-pass
Screenshot: C:\Users\...\grounding.png (marked)
```

### screenshot 输出

**默认保存路径：** `%USERPROFILE%\.agent-windows\screenshots\screenshot-<unix-timestamp-ms>.png`

固定目录，自动创建。文件名用毫秒时间戳，不互相覆盖。Daemon 启动时清理 7 天前的截图（可通过 `max_screenshot_age_days` 配置）。

**路径优先级（高到低）：**
1. 命令行直接指定：`agent-windows screenshot ./foo.png`
2. `--screenshot-dir` CLI 标志
3. `AGENT_WINDOWS_SCREENSHOT_DIR` 环境变量
4. 配置文件 `[screenshot] dir`
5. 默认 `%USERPROFILE%\.agent-windows\screenshots\`

**stdout 输出：**
```
## Screenshot
Path: C:\Users\<user>\.agent-windows\screenshots\screenshot-1749561234567.png
Size: 1920x1080
```

### observe 组合输出

`observe` 可组合 `--snapshot`、`--screenshot`、`--ground`，各模式按顺序拼接输出，用 `---` 分隔：

```
[Desktop State 段]
[Windows 表格]
[Accessibility Tree]
[Interactive Elements]
---

## Screenshot
Path: C:\Users\...\screenshot.png
Size: 1920x1080
---

## Grounding Result
Target: "保存按钮"
Coordinate: (520, 340)
...
```

不带任何标志的 `observe` 等价于 `observe --snapshot --screenshot`（默认返回 UIA 树 + 截图）。

### 多显示器

- `snapshot` / `observe` 报告所有显示器信息（数量、主显示器编号、各显示器分辨率）
- `screenshot` 默认截取所有显示器的虚拟屏幕（`GetVirtualScreenRect`），可通过 `--monitor <N>` 指定单个显示器
- 坐标使用虚拟屏幕坐标空间，左上角为 (0,0)，跨越多显示器自然延伸

## 视觉坐标定位 (Grounding)

### 两轮裁剪策略

移植自 open-apa 的 GroundingAgent，简化为工具函数：

1. **第一轮 (粗定位)：** 全屏截图 + 元素描述 → 视觉模型返回大致坐标 (x, y)
2. **第二轮 (精定位)：** 以第一轮坐标为中心裁剪 400x300 区域 → 视觉模型返回精确坐标
3. **验证：** 两轮结果偏差 >40px 则回退到第一轮坐标

### 坐标管道（完整流程）

```
原始截图 (物理像素，如 1920x1080)
    │
    ▼ 缩放到模型坐标空间 (1000x1000，保持宽高比，不足部分填充黑色)
模型输入图 (1000x1000)
    │
    ▼ 第一轮：视觉模型返回 (mx₁, my₁) ∈ [0, 1000]²
粗定位坐标 (模型空间)
    │
    ▼ 转换到物理像素：
    │   px = mx₁ * screen_width / model_width
    │   py = my₁ * screen_height / model_height
    │   （宽高比填充时，需补偿 padding 偏移）
粗定位坐标 (物理像素)
    │
    ▼ 裁剪区域：以 (px, py) 为中心，400x300
    │   边界钳制：
    │     crop_x = clamp(px - 200, 0, screen_width - 400)
    │     crop_y = clamp(py - 150, 0, screen_height - 300)
    │   若屏幕小于裁剪尺寸，缩小裁剪区域
裁剪图 (物理像素，400x300)
    │
    ▼ 缩放到模型坐标空间 (1000x1000)
模型输入图 (1000x1000)
    │
    ▼ 第二轮：视觉模型返回 (mx₂, my₂) ∈ [0, 1000]²
精定位坐标 (模型空间，相对裁剪区域)
    │
    ▼ 转换到物理像素：
    │   fine_x = mx₂ * 400 / 1000 + crop_x
    │   fine_y = my₂ * 300 / 1000 + crop_y
精定位坐标 (物理像素)
    │
    ▼ 偏差检查：
    │   distance = sqrt((fine_x - px)² + (fine_y - py)²)
    │   if distance > max_drift: final = (px, py)  // 回退粗定位
    │   else: final = (fine_x, fine_y)
最终坐标 (物理像素)
```

**模型输出解析：** 从视觉模型响应文本中用正则 `re.findall(r"\d+", response)` 提取数字。取前两个数字作为 (x, y)。若解析失败，返回错误 `-1005`。

### 模型支持

支持多种视觉模型 API，通过配置文件指定：

- OpenAI 兼容 API（覆盖 Doubao、Gemini 兼容端点等）
- Anthropic Claude
- Google Gemini

### Prompt 设计

#### 默认提示词

**元素坐标定位（第一轮粗定位）：**
```
Query:{description}
Output only the coordinate of one point in your response.
```

**元素坐标定位（第二轮精定位）：**
```
Query:{description}
The previous coarse location is ({x},{y}). Crop from ({cx},{cy}).
Output only the coordinate of one point in your response.
```

**文本短语定位（phrase-to-word，OCR 辅助）：**
```
You are an expert in graphical user interfaces. Your task is to process a phrase of text, and identify the most relevant word on the computer screen.
You are provided with a phrase, a table with all the text on the screen, and a screenshot of the computer screen. You will identify the single word id that is best associated with the provided phrase.
This single word must be displayed on the computer screenshot, and its location on the screen should align with the provided phrase.
Each row in the text table provides 2 pieces of data in the following order. 1st is the unique word id. 2nd is the corresponding word.

To be successful, it is very important to follow all these rules:
1. First, think step by step and generate your reasoning about which word id to click on.
2. Then, output the unique word id. Remember, the word id is the 1st number in each row of the text table.
3. If there are multiple occurrences of the same word, use the surrounding context in the phrase to choose the correct one. Pay very close attention to punctuation and capitalization.
```

#### 自定义提示词

用户可通过配置文件覆盖默认提示词，适配不同视觉模型的指令风格：

```toml
[grounding.prompts]
# 元素坐标定位 - 第一轮（{description} 为用户输入的元素描述）
coarse = "Query:{description}\nOutput only the coordinate of one point in your response."

# 元素坐标定位 - 第二轮（{x},{y} 为粗定位坐标；{cx},{cy} 为裁剪区域左上角）
fine = "Query:{description}\nThe previous coarse location is ({x},{y}). Crop from ({cx},{cy}).\nOutput only the coordinate of one point in your response."

# 文本短语定位（{phrase} 为目标短语；{ocr_table} 为 OCR 文字表）
phrase_to_word = """You are an expert in graphical user interfaces..."""
```

也可通过文件路径指定（方便长提示词和版本管理）：

```toml
[grounding.prompts]
coarse_file = "~/.agent-windows/prompts/coarse.txt"
fine_file = "~/.agent-windows/prompts/fine.txt"
phrase_to_word_file = "~/.agent-windows/prompts/phrase_to_word.txt"
```

优先级：`xxx_file` > `xxx` 内联 > 内置默认。

## 技术栈

- **语言：** Python 3.13+
- **CLI 框架：** Typer（比 Click 更现代，自动生成帮助文档）
- **COM 互操作：** comtypes + pywin32（复用 Windows-MCP uia/ 模块）
- **截图：** dxcam（优先）→ mss → Pillow（降级）
- **图像处理：** Pillow
- **视觉模型 API：** openai（兼容多种 API）+ anthropic + google-generativeai
- **包管理：** uv + setuptools
- **测试：** pytest

## 项目结构

```
agent-windows/
  src/agent_windows/
    __init__.py
    __main__.py              # 入口：判断 --daemon 模式还是 CLI 模式
    cli/
      __init__.py
      main.py                # Typer app + 命令注册
      commands/
        __init__.py
        observe.py           # observe/snapshot/screenshot/ground
        input.py             # click/type/scroll/move/shortcut
        system.py            # app/shell/fs/clipboard/process
        daemon_cmd.py        # daemon start/stop/status
        config_cmd.py        # config
        wait.py              # wait
    daemon/
      __init__.py
      server.py              # Daemon 主循环（Named Pipe 监听）
      router.py              # JSON-RPC 路由（命令名 → 处理函数）
      lifecycle.py           # Daemon 生命周期管理（启动/停止/空闲超时/PID 锁）
    core/
      __init__.py
      desktop.py             # 桌面编排服务
      tree.py                # UIA 无障碍树遍历
      screenshot.py          # 截图服务（多后端）
      grounding.py           # 视觉坐标定位（两轮裁剪）
      input_controller.py    # 鼠标/键盘控制
      app_manager.py         # 应用管理
      shell_runner.py        # PowerShell 执行
      filesystem.py          # 文件系统操作
      clipboard.py           # 剪贴板操作
      process_manager.py     # 进程管理
    ipc/
      __init__.py
      client.py              # CLI 端 IPC 客户端（连接 Daemon、发命令）
      protocol.py            # JSON-RPC 消息编解码
      pipe.py                # Windows Named Pipe 封装
    uia/                     # 复用自 Windows-MCP (vendored, 保留版权声明)
      __init__.py
      core.py
      controls.py
      patterns.py
      enums.py
      events.py
      exceptions.py
    utils/
      __init__.py
      config.py              # 配置管理 (TOML)
      image.py               # 图像处理工具
      api.py                 # 视觉模型 API 客户端
      coordinates.py         # 坐标转换工具
  pyproject.toml
  tests/
    conftest.py
    test_cli.py
    test_daemon.py
    test_ipc.py
    test_tree.py
    test_grounding.py
    test_screenshot.py
    test_input.py
    ...
```

## 配置

配置文件路径：`%USERPROFILE%\.agent-windows\config.toml`（即 `Path.home() / ".agent-windows" / "config.toml"`）

**数据目录统一在** `%USERPROFILE%\.agent-windows\`，包含：
- `config.toml` — 配置
- `daemon.pid` / `daemon.version` — Daemon 状态
- `logs/` — 日志
- `screenshots/` — 截图
- `prompts/` — 自定义提示词文件

```toml
[grounding]
enabled = true
engine = "openai"            # openai / anthropic / gemini
model = "gpt-4o-mini"
api_base = "https://api.openai.com/v1"
api_key = ""                 # 也可通过 AGENT_WINDOWS_GROUNDING_API_KEY 环境变量
width = 1000                 # 视觉模型坐标空间宽度
height = 1000                # 视觉模型坐标空间高度
crop_size = 400              # 第二轮裁剪宽度
crop_height = 300            # 第二轮裁剪高度
max_drift = 40               # 两轮最大允许偏差 (px)

[screenshot]
backend = "auto"             # auto / dxcam / mss / pillow
scale = 1.0
max_width = 1920
max_height = 1080
format = "png"               # png / jpeg
jpeg_quality = 85

[tree]
include_invisible = false
include_offscreen = false
max_depth = 30
```

环境变量覆盖优先级：CLI 参数 > 环境变量 > 配置文件 > 默认值

## 依赖

核心依赖：
- typer >= 0.12
- comtypes >= 1.4
- pywin32 >= 306
- Pillow >= 10.0
- dxcam >= 0.3 (可选)
- mss >= 10.0
- openai >= 1.0 (视觉定位)
- anthropic >= 0.40 (视觉定位，可选)
- toml >= 0.10

## 复用策略

### 从 Windows-MCP 复用

- `uia/` 模块整体 vendored：COM 初始化、鼠标/键盘模拟、UIA 控件封装、Win32 API 封装
- `tree/` 模块参考重写：树遍历逻辑参考，但简化接口
- `desktop/screenshot.py` 截图后端逻辑参考

### 从 open-apa 复用

- GroundingAgent 的两轮裁剪策略
- 图像编码和缩放逻辑
- 视觉模型 API 调用模式

### 从 agent-browser 参考

- CLI 命令设计模式
- 面向 AI 的输出格式
- 命令解析结构

## 阶段规划

### Phase 1: 核心骨架 + Daemon
- 项目初始化 (pyproject.toml, 目录结构)
- Daemon 框架（Named Pipe 监听、JSON-RPC 协议、三线程模型、生命周期管理）
- CLI 框架 (Typer app) + IPC 客户端
- vendored uia/ 模块（适配：移除模块级 DPI 调用、确保 COM init 在 STA 线程触发）
- 配置管理
- 端到端验证：CLI → IPC → Daemon → 响应

### Phase 2: 观察能力
- screenshot 命令
- snapshot 命令 (UIA 树遍历)
- observe 综合命令

### Phase 3: 输入能力
- click / type / scroll / move / drag / shortcut 命令
- 输入控制器

### Phase 4: 视觉定位
- grounding 命令
- 视觉模型 API 客户端
- 两轮裁剪策略实现

### Phase 5: 系统命令
- app / shell / fs / clipboard / process / wait

### Phase 6: 完善
- 测试覆盖
- 文档
- 错误处理和边界情况
