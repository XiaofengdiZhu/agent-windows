# TODO: Commands to Implement

Reference: agent-browser commands not yet in agent-windows.

## High Priority

- [x] **`dblclick`** — 双击。桌面常见操作（打开文件、选词），目前只有单击。实现简单（`MOUSEEVENTF_DOUBLECLICK`）。
- [x] **`hover`** — 鼠标悬停。触发工具提示、展开子菜单，目前 `move` 不会触发悬停事件。
- [ ] **`wait` 增强** — 等待元素/文字出现。当前 `wait` 只支持秒数，实际需要等 UI 变化（窗口出现、文字消失等）。
- [x] **`get text`** — 提取屏幕区域文字。从 UIA 树 提取指定区域文本，比 snapshot 整棵树更精准。（暂时不实现使用一般 OCR 工具）
- [x] **`highlight`** — 高亮标注元素。调试利器，在截图上标注目标位置。
- [x] **`batch`** — 批量执行命令。一次 IPC 发多条命令，减少往返开销。
- [x] **`mouse down/up`** — 独立的鼠标按下/释放。精细控制：长按、自定义拖拽序列。

## Medium Priority

- [x] **`is visible/enabled`** — 检查元素状态。UIA snapshot 里能判断，但专用命令更高效。
- [x] **`clipboard copy/paste`** — 添加模拟 Ctrl+C/V 的选项，能通过配置文件固化；默认仍走系统 API。某些应用只认键盘操作，不走系统 API。
- [x] **`diff screenshot`** — 视觉差异对比。对比两次截图的像素差异，可用于 UI 回归测试。
- [x] **`screenshot --selector`** — 元素级截图。只截某个 UIA 元素区域，减少图片尺寸和 token 消耗。
