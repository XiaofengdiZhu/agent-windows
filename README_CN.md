# agent-windows

面向 AI Agent 的 Windows 电脑使用 CLI。通过快速的 客户端-守护进程（Client-Daemon）架构，提供桌面截图、UIA 无障碍树遍历、视觉坐标定位以及鼠标/键盘自动化能力。

## 特性亮点
- **无障碍树 + 视觉定位** 双方案定位元素坐标，像素级精确；无障碍树方案完全不需要视觉模型。
- **给无视觉的 agent 装上眼睛** 驱动模型（DeepSeek、GLM 等）无多模态能力时，屏幕读取交给任意廉价/免费/本地视觉模型。上下文里只走坐标，不塞整张截图——省 token 降成本。
- **高速 Rust CLI** 比传统 Python CLI 快约 10 倍

## 安装

需要 [uv](https://docs.astral.sh/uv/)（下面使用的 Python 包运行器）。若尚未安装，先装它：

```bash
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

然后安装 agent-windows：

```bash
uv tool install agent-windows
```

或一次性试用（无需安装直接运行）：

```bash
uvx agent-windows observe
```

> **日常使用推荐 `uv tool install` 而非 `uvx`。** `uvx` 从缓存运行，缓存可能被静默刷新，导致守护进程版本与 CLI 漂移时触发不必要的重启。详见[疑难排查](skills/agent-windows/references/troubleshooting.md#daemon-keeps-restarting-uvx-version-drift)。

从源码安装（本仓库的检出）：

```bash
uv sync
uv run agent-windows ...
```

从源码重新构建原生启动器（需 Rust + MSVC + Windows SDK）：

```bash
cd src/launcher && build.cmd build --release   # 暂存 prebuilt/agent-windows.exe（已 gitignore）
```


## 快速开始

```bash
# 初始化配置
agent-windows config init

# 抓取截图
agent-windows screenshot

# 获取 UIA 树
agent-windows snapshot

# 组合观察（snapshot + screenshot）
agent-windows observe

# 视觉定位（按描述定位元素）
agent-windows ground "the taskbar"

# 鼠标/键盘
agent-windows click 500 300
agent-windows type "Hello World" --enter
agent-windows shortcut ctrl+c

# 系统
agent-windows app list
agent-windows shell "Get-Process | Select-Object -First 5"
agent-windows batch --file commands.json
```

## 架构

```
agent-windows.exe (原生 Rust 启动器, ~12ms)
    │
    ▼ Named Pipe IPC (JSON-RPC 2.0)
    │
Python daemon (持久化, COM STA worker)
```

- **原生启动器**（`agent-windows.exe`）：Rust 二进制，解析 argv 并把 JSON-RPC 请求转发给守护进程。源码：`src/launcher/`。它还把 `config` 委派给 Python CLI（逐字转发 argv），因此可以直接用 `agent-windows config ...` 来操作配置。`--no-daemon` 模式下，Python CLI 仍可直接以 `agent-windows-python` 调用。
- **守护进程（Daemon）**：持久化的 Python 进程，持有重型依赖（comtypes、uia、Pillow）。处理器按领域组织在 `daemon/handlers/` 中。
- **IPC**：带每用户 DACL 安全策略的 Named Pipe。JSON-RPC 2.0 协议。
- **自动启动**：首次 CLI 调用会自动启动守护进程。空闲超时（30 分钟）后停止。
- **串行执行**：一次只运行一条命令——UIA/COM 绑定到所在 apartment，且 `SendInput` 修改全局 GUI 状态，并发命令会相互竞争。仅当多个 Agent 共享一个守护进程时有影响（它们之间被串行化）；需要隔离时运行独立守护进程（每个 Agent 一个用户账号——管道路径按用户隔离）。

## 命令

完整参数与示例见[命令参考](skills/agent-windows/references/commands.md)；何时用哪个见 [Agent 技能](skills/agent-windows/SKILL.md)。运行 `agent-windows --help` 可快速查看命令清单。

### 观察（Observation）
| 命令 | 说明 |
|---------|-------------|
| `observe` | 组合观察（snapshot + screenshot + 可选视觉定位） |
| `snapshot` | UIA 无障碍树；`--window`/`--active` 限定单窗口 |
| `list-displays` | 列出显示器（序号、主显、分辨率、矩形、工作区、设备名），供 `-d N` |
| `screenshot` | 屏幕抓取；裁剪、叠加 `@eN` 编号框、参考网格 |
| `ground` | 视觉坐标定位（`--single-pass`：1 次 API 调用，更快、精度较低） |
| `detect` | 多目标边界框检测 |
| `find-image` | 在屏幕上定位已保存模板图——确定性、不调用视觉模型 |
| `pixel-at` | 读取屏幕某点颜色；`--match` 按颜色判断状态——确定性 |
| `ask-vision` | 自由形式视觉问答 |
| `highlight` | 在截图上高亮区域 |
| `diff` | 像素级截图差异 |
| `snapshot-diff` | 将保存的 UIA 树基线与当前对比（新增/移除/变更的元素） |
| `get-text` | 获取某 UIA 元素的文本内容 |
| `is visible/enabled` | 检查元素状态 |

### 输入（Input）
| 命令 | 说明 |
|---------|-------------|
| `click` | 鼠标点击（坐标或 `@eN`）；`--background` 不抢焦点点击（UIA pattern；不支持则 `-1007`） |
| `type` | 输入文本（Unicode 直接输入） |
| `shortcut` | 键盘快捷键（如 `ctrl+c`） |
| `key-down` / `key-up` | 原始按键按下/释放；`--hold N` 自动释放，否则成对使用 |
| `expand` / `collapse` | 展开/折叠元素（`@eN`）——ComboBox/TreeItem/Menu |
| _其它_ | `dblclick`、`hover`、`scroll`、`move`、`drag`、`mouse-down`、`mouse-up` |

### 系统（System）
| 命令 | 说明 |
|---------|-------------|
| `app launch` | 按名称启动应用（开始菜单 friendly name、UWP/Store 应用）、exe 或路径 |
| `app switch` | 切换到某窗口 |
| `app resize` | 调整窗口大小/位置 |
| `app list` | 列出可见窗口 |
| `window minimize/maximize/restore/close/focus` | 窗口控制 |
| `window enable` | 恢复被中断的 `--background` 点击所冻结的窗口 |
| `shell` | 运行 PowerShell 命令（UTF-8 输出） |
| `fs read/write/list/copy/move/delete/search/info` | 文件系统操作 |
| `clipboard get/set` | 剪贴板访问（`--keyboard` 模拟 Ctrl+C/V） |
| `process list` | 列出运行中的进程 |
| `process kill` | 终止进程（优雅或 `--force`） |
| `notify` | Windows toast 通知 |
| `registry get/set/delete/list` | Windows 注册表操作（类型化值） |
| `lock` | 锁定工作站（Win+L） |
| `sleep` | 使系统进入睡眠（S3） |
| `shutdown` | 关机（`--delay N` 0=立即 最大 600，`--force` 跳过保存对话框） |
| `restart` | 重启（`--delay N`，`--force`） |
| `cleanup` | 清理旧的/过大的截图 |
| `wait` | 等待时长、元素或文本 |
| `wait-for` | 等待某 UI 谓词成立 |
| `batch` | 一次 IPC 往返执行多条命令 |
| `multi-select` | 点击多个目标（默认按住 Ctrl） |
| `multi-edit` | 一次调用写入多个字段 |

### 守护进程与配置（Daemon & Config）
| 命令 | 说明 |
|---------|-------------|
| `daemon start/stop/status` | 守护进程管理 |
| `ping` | 守护进程健康检查（单次 IPC 往返） |
| `config init/show/set/doctor` | 配置（委派给 Python CLI） |
| `audit verify` | 校验破坏性操作审计日志完整性（仅 CLI） |
| `mcp serve/auth/install/uninstall` | MCP 服务端（委派给 Python CLI） |

### 全局选项（Global Options）
| 标志 | 说明 |
|------|-------------|
| `--no-daemon` | 直接执行（绕过守护进程，用于调试） |
| `--format text\|json` | 输出格式 |
| `--debug` | 详细日志 |
| `--config <path>` | 备用 config.toml（默认 `~/.agent-windows/config.toml`）。会传递给守护进程；读取*与*写入（`config set`/`mcp auth`）都指向此文件。状态文件（pid/lock/logs）仍留在默认位置。 |

## MCP 服务端

agent-windows 还内置了完整的 [MCP](https://modelcontextprotocol.io/) 服务端（基于 [FastMCP](https://github.com/jlowin/fastmcp) 构建），把各类自动化命令都暴露为 MCP 工具。任何支持 MCP 的客户端——Claude Desktop、Cursor、Gemini CLI 等——都能通过它来驱动 Windows。**本项目以 CLI 优先；MCP 是替代接口。** 完整指南——客户端安装配置、远程 HTTP 访问、鉴权、传输方式以及工具目录——见 **[MCP.md](MCP.md)**。

30 秒速览（本地 stdio，例如用于 Claude Desktop）：

```bash
agent-windows mcp serve          # 首次工具调用时自动启动守护进程
```

```json
{
  "mcpServers": {
    "agent-windows": { "command": "agent-windows", "args": ["mcp", "serve"] }
  }
}
```

> 需要 `mcp` extra：`uv tool install "agent-windows[mcp]"`（裸 `uv tool install agent-windows` 只拉取核心依赖）；从源码运行请用 `uv sync --extra mcp`。

## Agent 技能（Agent Skill）

仓库自带一份现成的 agent 技能：[`skills/agent-windows/SKILL.md`](skills/agent-windows/SKILL.md)（含 `references/`）。它是任何驱动本 CLI 的 agent 的使用指南：`observe → 找 @eN → 操作 → 验证` 核心循环、`@eN` 选择器体系及其失效规则、snapshot/ground/ask-vision 决策矩阵、按错误码的恢复步骤，以及可直接运行的工作流。

**任何走 CLI 的 agent 都应装上。** 没有它，agent 只能看到 `--help`，得自己重新摸索核心循环、选择器生命周期和错误处理。**若 agent 走 [MCP 服务端](#mcp-服务端)，则无需安装**——MCP 客户端直接拿到工具，不运行 CLI。

```bash
# Claude Code（macOS / Linux / Git Bash）：
cp -r skills/agent-windows ~/.claude/skills/
# Claude Code（PowerShell / Windows）：
Copy-Item -Recurse skills/agent-windows $HOME/.claude/skills/
```

想要项目级而非用户级：复制到 `.claude/skills/`。其他 agent 运行时，把 `skills/agent-windows/SKILL.md` 作为技能或系统提示词引入即可。

## 配置视觉模型（可选，推荐）

`ground`、`detect` 和 `ask-vision` 需要一个多模态模型来读取屏幕。用 `config set` 配置一个：

```bash
# 1. API key（或改用 AGENT_WINDOWS_VLM_API_KEY 环境变量）
agent-windows config set vlm.api_key sk-xxxxxxxx

# 2. 引擎：openai（默认）/ anthropic / gemini
agent-windows config set vlm.engine openai

# 3. 模型（一个廉价多模态模型即可）
agent-windows config set vlm.model gpt-4o-mini

# 4. 验证
agent-windows config doctor
```

> 非 OpenAI 端点需另设 `vlm.api_base`（如 `agent-windows config set vlm.api_base https://your-endpoint/v1`）。

## 配置

配置文件：`~/.agent-windows/config.toml`（自动创建，仅 owner 权限）

> **路径解析**：`[mcp] ssl_certfile` / `ssl_keyfile` 的相对路径相对**配置文件所在目录**解析，而非工作目录。绝对路径与 `~` 开头路径按原样使用。

> **安全**：`vlm.api_key` 静态存储时用 Windows DPAPI（**当前用户**范围）**加密**；配置文件 owner-only ACL。通过 `config set vlm.api_key sk-...` 或 `AGENT_WINDOWS_VLM_API_KEY` 环境变量设置（后者优先级更高，绝不写入磁盘）。`config show` 中 key 自动打码。

> **变更何时生效**：守护进程每条命令重新读取配置（检查 mtime），故 `config set` 对**下一条命令**生效——无需重启。唯一例外：`daemon.idle_timeout_minutes` 启动时读一次。

```toml
[vlm]
enabled = true
engine = "openai"       # openai / anthropic / gemini（+ doubao / gemini_compatible → 走 OpenAI-compatible）
model = "gpt-4o-mini"
api_base = "https://api.openai.com/v1"
api_key = ""             # 或 AGENT_WINDOWS_VLM_API_KEY 环境变量
timeout = 30
image_format = "JPEG"   # JPEG 或 PNG
jpeg_quality = 85        # JPEG 质量 (1-95)，image_format = PNG 时忽略

[screenshot]
backend = "auto"         # auto / dxcam / mss / pillow
max_width = 1920
max_height = 1080
resampling = "BILINEAR"  # BILINEAR / LANCZOS / NEAREST

[tree]
max_depth = 15         # 最大遍历深度
max_elements = 200     # 每次 snapshot 的可交互元素上限（0 = 禁用）
timeout = 10           # 全局遍历预算，秒（0 = 禁用）

[clipboard]
method = "system"      # "system"（PowerShell API）或 "keyboard"（模拟 Ctrl+C/V）

[shell]
timeout = 30             # 执行超时，秒（可逐次用 --timeout 覆盖）
max_output_bytes = 1048576  # stdout+stderr 上限（1 MiB）；0 = 不限；超限即终止命令
output_encoding = ""     # "" = UTF-8（自动）；否则如 "gbk" 用于旧代码页 exe
```

### 环境变量

下面每个选项都可以作为环境变量设置，替代（或补充）配置文件。所有地方的优先级都是 **标志 > 环境变量 > 配置**。

| 变量 | 领域 | 用途 |
|---|---|---|
| `AGENT_WINDOWS_VLM_API_KEY` | VLM | 视觉模型 API key；优先级高于 `[vlm] api_key`，绝不写入磁盘。 |
| `AGENT_WINDOWS_SCREENSHOT_DIR` | 截图 | 覆盖截图保存位置（默认 `~/.agent-windows/screenshots/`）。 |
| `AGENT_WINDOWS_DEBUG` | 调试 | 由 `--debug` 设置；模块用 `is_debug()` 读取，守护进程自动继承。 |
| `AGENT_WINDOWS_TELEMETRY` | 遥测 | `1` → 仅本地的工具调用日志（`logs/telemetry.log`，无网络）。 |
| `AGENT_WINDOWS_PYTHON` | 启动器 | 为守护进程显式指定 Python 解释器。 |
| `AGENT_WINDOWS_E2E` | 测试 | `1` → 启用端到端守护进程冒烟测试（`@pytest.mark.e2e`）。 |
| MCP 服务端变量 | MCP | `ALLOW_INSECURE_REMOTE`、`IP_ALLOWLIST`、`CORS_ORIGINS`、`TOOLS`、`EXCLUDE_TOOLS`、`STATELESS_HTTP`、`FILE_TRANSFER`、`MAX_UPLOAD_MB`、`OAUTH_CLIENT_ID`/`SECRET` —— 见 [MCP.md](MCP.md#http-security--scaling-options)。 |

## 运行环境要求

- Windows 8.1 / 10 / 11
- Python 3.13+
- 依赖：typer、comtypes、pywin32（≥306；推荐 ≥308）、Pillow、mss、openai、toml、numpy
- 可选：fastmcp（≥3.0，用于 MCP 服务端——`uv tool install "agent-windows[mcp]"` / `uv sync --extra mcp`）、dxcam、anthropic、google-genai

> **pywin32**：wheel（≥308）安装时通过 `.pth` 引导自动配置 DLL，`uv tool install` / `uvx` 开箱即用。较旧的 pywin32 或手工管理环境若缺 `pythoncom3xx.dll` / `pywintypes3xx.dll`，运行 `python Scripts\pywin32_postinstall.py -install`。

## 许可证

agent-windows 采用 **MIT License** 授权（见根目录 `LICENSE`）。

`src/agent_windows/uia/` 下内置的 `uia` 模块派生自 [yinkaisheng/Python-UIAutomation-for-Windows](https://github.com/yinkaisheng/Python-UIAutomation-for-Windows)，该仓库为 **Apache License 2.0**——见该目录下的 `src/agent_windows/uia/LICENSE-APACHE.txt` 与 `NOTICE`。原始版权声明保留在每个派生文件中。另有一些模式改编自 [Windows-MCP](https://github.com/CursorTouch/Windows-MCP)（MIT）。

## 贡献

见 [CONTRIBUTING.md](CONTRIBUTING.md)。安全问题请按 [SECURITY.md](SECURITY.md) 流程报告。
