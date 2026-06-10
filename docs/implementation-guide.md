# agent-windows 实现指南

本文档是设计文档 (`docs/agent-windows-design.md`) 的配套实现指南，包含设计文档未覆盖的实践细节、参考代码定位、已知陷阱和推荐实现顺序。

## 开发与测试环境

- **同一台 Windows 机器** 同时作为开发机和测试机
- **开发 Agent** 是 Claude Code
- **⚠️ 重要警告：** agent-windows 的鼠标/键盘自动化会控制真实桌面。**绝对不要关闭 Claude Code 所在的终端窗口**，否则会失去对开发 Agent 的控制。建议将 Claude Code 终端放在不会被自动化操作影响的显示器/位置上。
- **测试时保持警惕：** 自动化操作可能点击到任意位置。建议在测试前保存所有工作，关闭不需要的窗口。

## 视觉定位模型配置

使用讯飞星火提供的免费视觉模型（来自 `reference/open-apa/conf/conf.yaml`）：

```toml
[grounding]
enabled = true
engine = "openai"
model = "xopqwen36v35b"
api_base = "https://maas-api.cn-huabei-1.xf-yun.com/v2"
api_key = "1fa992f9847a0bc36106c25ad917d279:N2ZhZGQwM2M0Mzk3OGFjYWRiYmFhOTlm"
width = 1000
height = 1000
```

这个模型免费，可随意用于测试和开发。

## 最终目标：通过 OSWorld 测试

**终极目标是用 Claude Code + agent-windows 完成大多数 OSWorld benchmark 测试。**

### OSWorld 简介

OSWorld 是桌面 GUI 自动化 benchmark，在虚拟机中运行 Ubuntu 桌面环境，给 agent 一个任务指令，评估完成度。

### 环境配置参考

参考 `reference/open-apa/benchmark_test/osworld_setup/`，包含：
- `README.md` — 设置说明
- `lib_run_single.py` — 单任务执行循环（agent loop）
- `run.py` — 完整 benchmark 评估入口（多进程、容错、断点续跑）

### OSWorld 关键架构

1. `DesktopEnv` 管理 VM/容器，提供 `reset()`、`step()`、`evaluate()` 接口
2. Agent 通过 pyautogui 代码字符串与桌面交互
3. 观察：截图 + 可选 a11y 树
4. 评估：`env.evaluate()` 返回 0/1 分数
5. 支持多后端：VMware、Docker、AWS、Azure、GCP、VirtualBox

### agent-windows 的 OSWorld 适配思路

OSWorld 原生使用 pyautogui 代码字符串作为 action。agent-windows 的定位是替代 open-apa 的底层工具层，需要：

1. **方式 A（推荐）：** 编写 OSWorld 适配层，将 Claude Code 的决策输出转为 agent-windows CLI 命令调用，而非 pyautogui 代码
2. **方式 B：** 在 OSWorld 的 `lib_run_single.py` 中，将 `env.step(pyautogui_code)` 替换为调用 agent-windows CLI 命令

两种方式都需要：
- 从 OSWorld VM 获取截图（VM 截图，非宿主机截图）
- 将截图传给 Claude Code 分析（通过 Claude Code 自身的视觉能力或 agent-windows 的 ground 命令）
- 执行动作（click/type/scroll 等）发送到 VM 内部

**注意：** agent-windows 当前设计控制的是**宿主机桌面**。要控制 OSWorld VM 内部桌面，可能需要：
- 在 VM 内安装 agent-windows Daemon
- 或通过 VM 提供商的 API（如 VMware 的 VIX、Docker exec）转发命令
- 或使用 OSWorld 自带的 `env.step()` 机制但用 agent-windows 格式

这部分架构需要在 Phase 6 之前细化。

## 参考代码关键定位

实现时需要频繁参考 `reference/` 目录下三个项目。以下是最关键的文件和函数：

### Windows-MCP（核心复用源）

| 文件 | 关键内容 | 复用方式 |
|------|----------|----------|
| `reference/Windows-MCP/src/windows_mcp/uia/core.py` | COM 初始化、鼠标/键盘模拟、Win32 API 封装、DPI 设置（~2500 行）| **整体 vendored** |
| `reference/Windows-MCP/src/windows_mcp/tree/service.py` | UIA 树遍历、元素分类、浏览器检测、Firefox IA2 降级 | **参考重写** |
| `reference/Windows-MCP/src/windows_mcp/tree/config.py` | 控件类型分类表（interactive/scrollable/informative/structural） | **直接复用** |
| `reference/Windows-MCP/src/windows_mcp/tree/views.py` | TreeElementNode、SemanticNode、BoundingBox 数据模型 | **参考重写** |
| `reference/Windows-MCP/src/windows_mcp/tree/cache_utils.py` | UIA CacheRequest 批量属性获取 | **直接复用** |
| `reference/Windows-MCP/src/windows_mcp/desktop/service.py` | 桌面编排：截图+树+窗口管理的整合 | **参考重写** |
| `reference/Windows-MCP/src/windows_mcp/desktop/screenshot.py` | 多后端截图（dxcam→mss→Pillow） | **参考重写** |
| `reference/Windows-MCP/src/windows_mcp/tools/_snapshot_helpers.py` | Snapshot 响应构建逻辑 | **参考** |
| `reference/Windows-MCP/src/windows_mcp/tools/input.py` | Click/Type/Scroll/Move 命令的参数处理 | **参考** |

### open-apa（视觉定位复用源）

| 文件 | 关键内容 | 复用方式 |
|------|----------|----------|
| `reference/open-apa/agent_core/agents/grounding_agent.py` | `get_element_coordinate()` 两轮裁剪实现 | **移植简化** |
| `reference/open-apa/agent_core/agents/system_prompts.py` | 所有提示词模板 | **参考** |
| `reference/open-apa/agent_core/lmms/session.py` | 图片编码、消息格式化、token 预算裁剪 | **参考** |

### agent-browser（CLI 架构参考）

| 文件 | 关键内容 | 复用方式 |
|------|----------|----------|
| `reference/agent-browser/cli/src/connection.rs` | Daemon 连接、重试、PID 检测、版本检测 | **参考模式** |
| `reference/agent-browser/cli/src/native/daemon.rs` | Daemon 启动、IPC 监听 | **参考模式** |
| `reference/agent-browser/cli/src/commands.rs` | CLI 命令解析为 JSON | **参考** |
| `reference/agent-browser/cli/src/native/screenshot.rs` | 截图保存路径逻辑（`get_screenshot_dir`） | **参考** |

## Vendoring uia/ 模块的必要修改

直接复制 Windows-MCP 的 `uia/` 目录后，必须做以下修改：

### 1. 移除模块级 DPI 调用

`uia/core.py` 中有模块级代码：
```python
SetProcessDpiAwareness(Process_DPI_Per_Monitor_Dpi_Aware)
```
这会在 import 时立即执行。**必须移除**，改为由 Daemon 在启动时显式调用一次。

### 2. 移除 `_AutomationClient` 中的 `CoInitialize`

`uia/core.py` 的 `_AutomationClient.__init__` 调用 `CoInitialize(None)`。由于我们的 Daemon 在专用 STA 线程上工作，这个调用可能冲突。需要改为：
- 在 `_AutomationClient` 中移除 `CoInitialize` 调用
- Daemon 的 STA 工作线程自己负责 `pythoncom.CoInitialize()` + 消息泵
- 或保留但确保只从 STA 线程首次触发

### 3. 清理不必要的依赖

vendored 模块可能引用了 Windows-MCP 的其他模块（如 config、paths 等）。需要逐个清理或替换为 agent-windows 自己的实现。

### 4. 保留版权声明

在 `uia/` 目录和文件头部保留原始版权声明，注明来源 `yinkaisheng/Python-UIAutomation-for-Windows` 和 Windows-MCP 项目。

## COM STA 线程实现要点

这是整个项目最复杂的部分。Python 中实现 COM STA 工作线程的推荐模式：

```python
import threading
import ctypes
import pythoncom
import win32con

class COMWorkerThread(threading.Thread):
    def __init__(self, command_queue, result_dict):
        super().__init__(daemon=True)
        self.command_queue = command_queue
        self.result_dict = result_dict
        self._ready = threading.Event()

    def run(self):
        # COM STA 初始化
        pythoncom.CoInitialize()
        # DPI 感知（如果主线程没做的话）
        # ctypes.windll.shcore.SetProcessDpiAwareness(2)

        # 主动触发 UIA 初始化（确保在 STA 线程上）
        # from agent_windows.uia.core import _AutomationClient
        # _AutomationClient.instance()

        self._ready.set()

        # 消息泵 + 命令处理循环
        while True:
            # 处理 Windows 消息（COM 需要）
            pythoncom.PumpWaitingMessages()

            # 检查命令队列（非阻塞）
            try:
                cmd = self.command_queue.get(timeout=0.05)
                result = self.execute_command(cmd)
                self.result_dict[cmd['id']] = result
            except queue.Empty:
                continue

        pythoncom.CoUninitialize()

    def wait_ready(self):
        self._ready.wait()
```

**关键点：**
- `pythoncom.PumpWaitingMessages()` 必须定期调用，否则 COM 调用会死锁
- 队列轮询间隔不能太长（50ms 是合理的），否则 COM 消息处理延迟
- `_AutomationClient.instance()` 必须在此线程首次调用

## Named Pipe 服务端实现

Python 中 Windows Named Pipe 服务端：

```python
import win32pipe
import win32file

PIPE_NAME = r"\\.\pipe\agent-windows"
BUFFER_SIZE = 65536

def create_pipe_server():
    while True:
        pipe = win32pipe.CreateNamedPipe(
            PIPE_NAME,
            win32pipe.PIPE_ACCESS_DUPLEX,
            win32pipe.PIPE_TYPE_BYTE | win32pipe.PIPE_READMODE_BYTE | win32pipe.PIPE_WAIT,
            1,  # 最大实例数
            BUFFER_SIZE,
            BUFFER_SIZE,
            0,  # 默认超时
            None  # 默认安全描述符
        )
        # 等待客户端连接
        win32pipe.ConnectNamedPipe(pipe, None)
        # 读取请求
        _, data = win32file.ReadFile(pipe, BUFFER_SIZE)
        request = data.decode('utf-8').strip()
        # 处理并写入响应
        response = handle_request(request)
        win32file.WriteFile(pipe, response.encode('utf-8'))
        win32pipe.DisconnectNamedPipe(pipe)
```

**注意：** 每个 CLI 调用是一个独立连接。当前设计是串行处理（`PIPE_WAIT` + max_instances=1），适合 COM STA 的串行约束。

## 测试策略

### 单元测试（无需 Windows 桌面）

可在开发机上运行：
- JSON-RPC 协议编解码
- 命令行参数解析
- 配置文件读写
- 坐标管道计算（缩放、裁剪、边界钳制）
- 输出格式化

### 集成测试（需要 Windows 桌面）

必须在有桌面的 Windows 环境运行（远程机器）：
- Daemon 启动/停止/崩溃恢复
- Named Pipe IPC 端到端
- 截图捕获
- UIA 树遍历
- 鼠标/键盘操作
- 视觉定位（需要网络访问视觉模型 API）

### 推荐测试流程

1. **Phase 1 完成后**：手动测试 Daemon 启动 → CLI 发 `ping` 命令 → 收到响应。验证 IPC 管道。
2. **Phase 2 完成后**：`agent-windows screenshot` → 检查截图文件。`agent-windows snapshot` → 检查输出文本。
3. **Phase 3 完成后**：打开记事本，用 CLI 命令输入文字、点击菜单。
4. **Phase 4 完成后**：`agent-windows ground "文件菜单"` → 验证返回坐标是否准确。
5. **Phase 5 完成后**：端到端测试完整工作流。

## 实现优先级建议

设计文档定义了 6 个 Phase。以下是每个 Phase 中建议的实现顺序（从最简单验证到最复杂功能）：

### Phase 1（建议 3-5 天）
1. 项目骨架：pyproject.toml + 目录结构 + Typer CLI
2. `config` 命令：init/show/set
3. IPC 层：Named Pipe 客户端 + 服务端 + JSON-RPC 编解码
4. Daemon 框架：启动、PID 文件、生命周期管理
5. 三线程模型：I/O 线程 + COM STA 工作线程 + 空闲监控
6. `ping` 命令作为端到端验证
7. Vendoring uia/ 模块 + 适配修改
8. `daemon start/stop/status` 命令

### Phase 2（建议 2-3 天）
1. `screenshot` 命令（最简单的观察命令）
2. `snapshot` 命令（UIA 树遍历，最复杂）
3. `observe` 综合命令

### Phase 3（建议 1-2 天）
1. `click` 命令
2. `shortcut` 命令
3. `type` 命令
4. `move` / `drag` 命令
5. `scroll` 命令

### Phase 4（建议 2-3 天）
1. 视觉模型 API 客户端（OpenAI 兼容优先）
2. `ground` 命令（单轮定位，先不做两轮）
3. 两轮裁剪策略
4. 自定义提示词支持

### Phase 5（建议 2-3 天）
1. `shell` 命令
2. `process` 命令
3. `clipboard` 命令
4. `fs` 命令
5. `app` 命令（最复杂，涉及窗口管理和应用发现）

### Phase 6（持续）
- 测试覆盖
- 错误处理和边界情况
- 文档和 README

## 已知陷阱

### 1. COM 线程死锁
**症状：** Daemon 挂起，CLI 超时。
**原因：** STA 线程没有调用消息泵，COM 调用被阻塞。
**预防：** STA 工作线程必须定期调用 `pythoncom.PumpWaitingMessages()`。

### 2. DPI 坐标不一致
**症状：** 点击位置偏移，UIA 坐标和鼠标坐标对不上。
**原因：** DPI 感知未设置，UIA 返回逻辑像素，鼠标需要物理像素。
**预防：** Daemon 启动时立即设置 DPI 感知。

### 3. UIA 树遍历超时
**症状：** `snapshot` 命令耗时数秒甚至超时。
**原因：** 某些应用的 UIA 提供程序响应慢（如 Visual Studio）。
**预防：** 设置每个窗口的遍历超时（参考 Windows-MCP 的 `THREAD_MAX_RETRIES`），超时跳过该窗口。

### 4. Named Pipe 残留
**症状：** CLI 连接失败，提示 pipe 已存在但无响应。
**原因：** Daemon 崩溃后 Named Pipe 未清理。
**预防：** 启动时检查 PID 文件和进程存活状态，清理残留。

### 5. 视觉模型返回格式不一致
**症状：** `ground` 命令解析失败。
**原因：** 不同模型返回坐标格式不同（`(x, y)` / `x, y` / 自然语言）。
**预防：** 使用宽松正则 `re.findall(r"\d+", response)` 提取数字。自定义提示词时可在提示词中明确格式要求。

### 6. Firefox 的 UIA 支持不完整
**症状：** Firefox 窗口的 UIA 树为空或缺失元素。
**原因：** Firefox 使用 IAccessible2 而非标准 UIA。
**预防：** 参考 Windows-MCP 的 `tree/ia2.py`，检测 Firefox 窗口时降级到 IA2 遍历。

## Python 依赖说明

| 包 | 安装 | 注意 |
|----|------|------|
| `typer` | `uv add typer` | CLI 框架 |
| `comtypes` | `uv add comtypes` | COM 互操作 |
| `pywin32` | `uv add pywin32` | Win32 API（Named Pipe、窗口管理等） |
| `Pillow` | `uv add pillow` | 图像处理 |
| `mss` | `uv add mss` | 截图（跨平台后备） |
| `dxcam` | `uv add dxcam` | DXGI 快速截图（可选，性能最佳） |
| `openai` | `uv add openai` | 视觉模型 API（兼容多平台） |
| `toml` | Python 3.11+ 内置 `tomllib` | 配置文件解析 |

**注意：** `pywin32` 安装后需运行 `python -m pywin32_postinstall -install`（通常 uv/pip 会自动处理）。
