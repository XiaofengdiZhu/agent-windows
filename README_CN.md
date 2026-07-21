# agent-windows

面向 AI Agent 的 Windows 电脑使用 CLI。通过快速的 客户端-守护进程（Client-Daemon）架构，提供桌面截图、UIA 无障碍树遍历、视觉坐标定位以及鼠标/键盘自动化能力。

## 特性亮点
- **给无视觉的 agent 装上眼睛。** 专为驱动模型（DeepSeek、GLM 等）无多模态能力，或宿主无法为单个工具配置视觉模型的场景而做——屏幕读取全部交给单独配置的视觉模型。
- **无障碍树+视觉定位** 双方案定位元素坐标/范围，像素级精确，前者无需视觉模型。
- **使用你自己的视觉模型** 配置任意廉价/免费/本地多模态模型，本工具会用它来视觉定位那些不在无障碍树中的元素，例如纯图标按钮、自绘 UI、画布。
- **节省 Tokens + 降低成本** 只是确定元素位置，完全不需要昂贵的多模态大型模型来进行，也不应该将整张截图塞入上下文，它需要的只是一个坐标。
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

> **日常使用推荐 `uv tool install` 而非 `uvx`。** 守护进程是长生命的，且当版本与 CLI 不一致时会自动重启；`uv tool install` 把两者版本一起锁定，而 `uvx` 从缓存运行，缓存可能被静默刷新（新版本发布、缓存过期、`uvx -U`），从而触发不必要的守护进程重启。详见[疑难排查：uvx 版本漂移](skills/agent-windows/references/troubleshooting.md#daemon-keeps-restarting-uvx-version-drift)。

从源码安装（本仓库的检出）：

```bash
uv sync
uv run agent-windows ...
```

从源码重新构建原生启动器（需要 Rust + MSVC + Windows SDK）：

```bash
cd src/launcher && build.cmd build --release
# exe 会暂存到 prebuilt/（已 gitignore），并由 `uv build` 拾取
# 发布版在 CI 中从源码构建——仓库中不提交任何二进制
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
# 单趟模式：一次 API 调用，约 2 倍速，精度较低（覆盖配置项 vlm.single_pass）
agent-windows ground "the taskbar" --single-pass

# 检测多个目标（返回边界框）
agent-windows detect "PDF file icons on the desktop"

# 视觉问答（自定义提示词 + 截图）
agent-windows ask-vision "Which windows are open? Is there an error dialog?"

# 鼠标/键盘
agent-windows click 500 300
agent-windows type "Hello World" --enter
agent-windows shortcut ctrl+c
agent-windows scroll 500 300 --direction down --amount 5

# 系统
agent-windows app list
agent-windows shell "Get-Process | Select-Object -First 5"
agent-windows fs list C:\\Users
agent-windows clipboard get
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
- **串行执行**：守护进程一次只运行一条命令。这是必须的，因为 UIA/COM 绑定到所在 apartment，且 `SendInput` 会修改全局 GUI 状态——并发命令会相互竞争。单客户端天然串行；唯一有影响的场景是多个 Agent 共享一个守护进程（它们之间会被串行化）。需要隔离时，请运行独立的守护进程（每个 Agent 一个用户账号——管道路径是按用户隔离的）。

## 命令

多数命令还有更多未列出的参数——运行 `agent-windows --help` 或查阅 [Agent 技能](skills/agent-windows/SKILL.md) 获取完整用法。

### 观察（Observation）
| 命令 | 说明 |
|---------|-------------|
| `observe [--snapshot] [--screenshot] [--ground DESC] [-d N]` | 组合观察（`-d`/`--display` 选择显示器，可重复） |
| `snapshot [-d N]` | UIA 无障碍树（`-d`/`--display` 选择显示器，可重复） |
| `screenshot [-o PATH] [--monitor N] [-s @eN] [--bbox X1,Y1,X2,Y2] [--annotate] [--grid-w N] [--grid-h N]` | 屏幕抓取；`--monitor -1`=全部；`--annotate` 叠加带编号的 `@eN` 框（全屏）+ 可选 `%` 参考网格 |
| `ground DESCRIPTION [--single-pass]` | 视觉坐标定位（`--single-pass`：1 次 API 调用，约 2 倍速，精度较低） |
| `detect DESCRIPTION [-i IMAGE] [--max-targets N]` | 多目标边界框检测 |
| `find-image TEMPLATE [--in SOURCE] [--bbox X1,Y1,X2,Y2] [-t N] [--grayscale] [--all]` | 在屏幕上定位已保存的模板图；确定性、不调用视觉模型。有目标截图（图标/字形）时使用。 |
| `pixel-at X Y [--in SOURCE] [--sample N] [--match COLOR] [-t N] [--grayscale]` | 读取屏幕某点的颜色；`--match` 按颜色判断状态（LED 亮灭、进度条填色）。确定性、不调用视觉模型。 |
| `ask-vision PROMPT [-i IMAGE] [-m MODEL]` | 自由形式视觉问答 |
| `highlight X1 Y1 X2 Y2 [-l LABEL] [-c COLOR]` | 在截图上高亮区域 |
| `diff BASELINE [-c COMPARE] [-t THRESHOLD]` | 像素级截图差异 |
| `get-text SELECTOR` | 获取某 UIA 元素的文本内容 |
| `is visible/enabled SELECTOR` | 检查元素状态 |

### 输入（Input）
| `click X Y \| -s @eN [--right] [--double]` | 鼠标点击 |
| `dblclick X Y \| -s @eN [--right]` | 双击 |
| `hover X Y \| -s @eN` | 悬停（触发工具提示/高亮） |
| `mouse-down X Y \| -s @eN [--button left\|right\|middle]` | 按住鼠标按键 |
| `mouse-up X Y \| -s @eN [--button left\|right\|middle]` | 释放鼠标按键 |
| `type TEXT [--at X,Y \| --ats @eN] [--enter] [--clear] [--caret start\|idle\|end]` | 输入文本（Unicode 直接输入） |
| `scroll X Y \| -s @eN [--direction up\|down] [--amount N]` | 滚动 |
| `move X Y \| -s @eN` | 移动鼠标 |
| `drag X1 Y1 X2 Y2 [--right]` | 拖拽 |
| `shortcut KEYS` | 键盘快捷键（如 `ctrl+c`） |
| `key-down KEYS [--hold N]` | 按下按键（原始 key-down）；`--hold N` 为按住 N 秒后自动释放，否则保持按下（配 `key-up`） |
| `key-up KEYS` | 释放按键（原始 key-up） |
| `expand SELECTOR` | 展开元素（@eN）——ComboBox/TreeItem/Menu |
| `collapse SELECTOR` | 折叠元素（@eN） |

### 系统（System）
| 命令 | 说明 |
|---------|-------------|
| `app launch NAME [ARGS...]` | 按名称启动应用（开始菜单 friendly name，含 UWP/Store 应用如 `calculator`）、exe 名或路径；末尾 token 作为参数 |
| `app switch NAME` | 切换到某窗口 |
| `app resize NAME --width W --height H [--x X --y Y]` | 调整窗口大小/位置（`--x`/`--y` 默认 `-1` = 保持不变） |
| `app list` | 列出可见窗口 |
| `shell COMMAND [--timeout SECS]` | 运行 PowerShell 命令（UTF-8 输出；`--timeout` 覆盖默认 30 秒） |
| `fs read PATH [--offset N] [--limit N] [--encoding ENC]` | 读取文件（`--encoding` 对旧代码页文件覆盖默认 UTF-8） |
| `fs write PATH CONTENT [--append]` | 写入文件 |
| `fs list PATH [--pattern GLOB] [--recursive] [--show-hidden]` | 列出目录 |
| `fs copy/move SRC DST [--overwrite]` · `fs delete PATH [--recursive]` · `fs search PATH PATTERN [--no-recursive]` · `fs info PATH` | 复制/移动/删除/搜索/信息 |
| `clipboard get/set [--keyboard]` | 剪贴板访问（`--keyboard` 模拟 Ctrl+C/V） |
| `process list [--sort-by memory\|cpu\|name] [--limit N] [--name SUBSTR]` | 列出运行中的进程 |
| `process kill PID\|NAME [--force]` | 终止进程（`--force` = 立即；默认优雅终止） |
| `notify TITLE MESSAGE [--app-id ID]` | Windows toast 通知（默认使用 PowerShell 的 AUMID） |
| `registry get/set/delete/list PATH [NAME] [VALUE] [--type T]` | Windows 注册表操作（类型化值：String/DWord/QWord/MultiString/Binary） |
| `cleanup` | 清理旧的/过大的截图 |
| `wait SECONDS|SELECTOR [--text TEXT] [--timeout N] [--interval N]` | 等待时长、元素或文本 |
| `wait-for CONDITION [--text S] [--window W] [--timeout N]` | 等待某 UI 谓词成立（`text_exists`/`active_window`/`element_exists`/`element_enabled`/`focused_element`；别名 `text`/`window`/`element`/`enabled`/`focused`） |
| `batch JSON/--file PATH [--delay N] [--continue-on-error]` | 一次 IPC 往返执行多条命令 |
| `multi-select JSON/--file PATH [--selectors] [--no-ctrl]` | 点击多个目标（默认按住 Ctrl） |
| `multi-edit JSON/--file PATH [--selectors]` | 一次调用写入多个字段 |

### 守护进程与配置（Daemon & Config）
| 命令 | 说明 |
|---------|-------------|
| `daemon start/stop/status` | 守护进程管理 |
| `ping` | 守护进程健康检查（单次 IPC 往返） |
| `config init/show/set/doctor` | 配置（委派给 Python CLI） |
| `mcp serve/auth/install/uninstall` | MCP 服务端（委派给 Python CLI） |

### 全局选项（Global Options）
| 标志 | 说明 |
|------|-------------|
| `--no-daemon` | 直接执行（绕过守护进程，用于调试） |
| `--format text\|json` | 输出格式 |
| `--debug` | 详细日志 |
| `--config <path>` | 备用 config.toml（默认 `~/.agent-windows/config.toml`）。会传递给守护进程；读取*与*写入（`config set`/`mcp auth`）都指向此文件。状态文件（pid/lock/logs）仍留在默认位置。 |

## MCP 服务端

agent-windows 还内置了完整的 [MCP](https://modelcontextprotocol.io/) 服务端（基于 [FastMCP](https://github.com/jlowin/fastmcp) 构建），把每条 CLI 命令都暴露为 MCP 工具。任何支持 MCP 的客户端——Claude Desktop、Cursor、Gemini CLI 等——都能通过它来驱动 Windows。**本项目以 CLI 优先；MCP 是替代接口。** 完整指南——客户端安装配置（含 MSIX/Store 的 Claude Desktop、Cursor、Perplexity、Gemini CLI、Qwen Code、Codex CLI、含 WSL 的 Claude Code）、远程 HTTP 访问、鉴权、传输方式以及工具目录——见 **[MCP.md](MCP.md)**。

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

> 需要 `mcp` extra：`uv tool install agent-windows` 默认会安装；从源码运行请用 `uv sync --extra mcp`。

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

> `config` 命令由启动器处理：它会透明地转发给内置的
> Python CLI（`agent-windows-python`），后者拥有配置 schema 和
> DPAPI 加密逻辑。无需单独的命令名。

## 配置

配置文件：`~/.agent-windows/config.toml`（自动创建，仅 owner 权限）

> **路径解析**：`[mcp] ssl_certfile` / `ssl_keyfile` 中的相对路径是相对**配置文件所在目录**解析，而不是进程工作目录——因此通过 `--config <path>` 指定的配置，会把它的 TLS 路径解析在它自己旁边。绝对路径和以 `~` 开头的路径按原样使用。若配置的 TLS 文件缺失，`mcp serve` 会在启动服务端前立即失败，并给出解析后的路径与配置位置。

> **安全**：`vlm.api_key` 在静态存储时用 Windows DPAPI（**当前用户**范围）**加密**；配置文件也通过 owner-only ACL 限定为仅你的 Windows 账号可访问。请始终通过 `config set vlm.api_key sk-...`（或 `AGENT_WINDOWS_VLM_API_KEY` 环境变量）来设置——保存时透明加密、加载时透明解密，你无需处理加密形式。`config show` 输出中的 API key 会自动打码。CI/CD 或共享环境中，推荐使用 `AGENT_WINDOWS_VLM_API_KEY` 环境变量——它优先级高于配置文件，且绝不写入磁盘。

> **变更何时生效**：守护进程在每条命令上重新读取配置文件（检查文件 mtime），因此 `config set` 对**下一条命令**生效——无需重启守护进程。唯一例外是 `daemon.idle_timeout_minutes`：它在守护进程启动时读取一次，修改它需要重启（`daemon stop` 然后 `daemon start`，或让旧的空闲超时退出）。

```toml
[vlm]
enabled = true
engine = "openai"       # openai / anthropic / gemini
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
max_output_bytes = 1048576  # stdout+stderr 上限（默认 1 MiB）；0 = 不限。输出增量读取，
                          # 一旦超过上限命令即被终止
                          #（限制守护进程内存并释放 worker，而非让失控命令跑满整个 timeout）
output_encoding = ""     # "" = UTF-8（自动）；否则如 "gbk" 用于旧代码页 exe
```

### 环境变量

下面每个选项都可以作为环境变量设置，替代（或补充）配置文件。所有地方的优先级都是 **标志 > 环境变量 > 配置**。按领域分组：

| 变量 | 领域 | 用途 |
|---|---|---|
| `AGENT_WINDOWS_VLM_API_KEY` | VLM / 配置 | 视觉模型 API key。优先级高于配置中的 `[vlm] api_key`；绝不写入磁盘。CI/共享环境请设置此项。 |
| `AGENT_WINDOWS_SCREENSHOT_DIR` | 截图 | 覆盖截图保存位置（默认 `~/.agent-windows/screenshots/`）。 |
| `AGENT_WINDOWS_ALLOW_INSECURE_REMOTE` | MCP / 安全 | 允许未鉴权的非环回 HTTP 绑定（`mcp serve`）。不推荐。 |
| `AGENT_WINDOWS_IP_ALLOWLIST` | MCP / 安全 | HTTP 服务端接受的逗号分隔 IP/CIDR；其他返回 403。 |
| `AGENT_WINDOWS_CORS_ORIGINS` | MCP / 安全 | 逗号分隔的允许跨域 URL。默认空（不发 CORS 头）。 |
| `AGENT_WINDOWS_TOOLS` | MCP / 安全 | 逗号分隔的工具**白名单**（覆盖 exclude）。被过滤掉的工具是被真正拦截，而非仅隐藏——`batch` 子调用同样会被过滤。有效名称见 [MCP.md](MCP.md#tools)。（此项过滤的是 MCP 服务端进程；守护进程单独执行 `[tools]` 配置项——见 [MCP.md](MCP.md#http-security--scaling-options)。） |
| `AGENT_WINDOWS_EXCLUDE_TOOLS` | MCP / 安全 | 从活跃集合中移除的逗号分隔工具（如 `shell,registry`）——在 MCP 层（含 `batch`）执行。要同时绑定直连 named-pipe 客户端，请在配置中设置 `[tools] exclude`（守护进程在启动时遵循它）。执行边界见 [MCP.md](MCP.md#http-security--scaling-options)。 |
| `AGENT_WINDOWS_STATELESS_HTTP` | MCP / 安全 | `1`/`true` → 无状态 streamable-http（不发 `Mcp-Session-Id`；可在重启后存活）。 |
| `AGENT_WINDOWS_OAUTH_CLIENT_ID` | MCP / 安全 | OAuth client id（需配合 `--oauth-client-secret`）。 |
| `AGENT_WINDOWS_OAUTH_CLIENT_SECRET` | MCP / 安全 | OAuth client secret。 |
| `AGENT_WINDOWS_TELEMETRY` | 遥测 | `1`/`true`/`enabled` → 启用仅本地的工具调用日志（默认关闭；写 `logs/telemetry.log`，无网络）。 |
| `AGENT_WINDOWS_PYTHON` | 启动器 | 为守护进程显式指定 Python 解释器（最高优先级；否则取 PATH 上的 `python`）。 |
| `AGENT_WINDOWS_E2E` | 测试 | `1` → 启用端到端守护进程冒烟测试（opt-in 的 `@pytest.mark.e2e`）。 |

> `--debug`（详细日志）会通过
> `enable_debug()` 设置 `AGENT_WINDOWS_DEBUG` 环境变量。任何模块都用
> `is_debug()` 读取它，而不是把 debug 标志当作参数传入；派生的守护进程会自动继承它。你也可以直接
> 导出 `AGENT_WINDOWS_DEBUG=1`。

## 运行环境要求

- Windows 8.1 / 10 / 11
- Python 3.13+
- 依赖：typer、comtypes、pywin32（≥306；推荐 ≥308）、Pillow、mss、openai、toml、numpy
- 可选：fastmcp（≥3.0，用于 MCP 服务端——`uv tool install agent-windows` / `uv sync --extra mcp`）、dxcam、anthropic、google-genai

> **pywin32 说明**：现代 pywin32 wheel（≥308）在安装时通过 `.pth` 引导脚本自动配置其 DLL，因此 `uv tool install` / `uvx` 开箱即用。若在较旧的 pywin32 或手工管理的环境中遇到 `pythoncom3xx.dll` / `pywintypes3xx.dll` 找不到，请在你的安装前缀下运行 `python Scripts\pywin32_postinstall.py -install`。

## 许可证

agent-windows 采用 **MIT License** 授权（见根目录 `LICENSE`）。

`src/agent_windows/uia/` 下内置的 `uia` 模块派生自 [yinkaisheng/Python-UIAutomation-for-Windows](https://github.com/yinkaisheng/Python-UIAutomation-for-Windows)，该仓库为 **Apache License 2.0**——见该目录下的 `src/agent_windows/uia/LICENSE-APACHE.txt` 与 `NOTICE`。原始版权声明保留在每个派生文件中。另有一些模式改编自 [Windows-MCP](https://github.com/CursorTouch/Windows-MCP)（MIT）。

## 贡献

见 [CONTRIBUTING.md](CONTRIBUTING.md)。安全问题请按 [SECURITY.md](SECURITY.md) 流程报告。
