---
name: easyeda-api
description: >-
  嘉立创EDA（EasyEDA）专业版 API 技能，供 AI 智能体使用。当用户需要操作 EasyEDA 专业版
  EDA 软件时使用，涵盖 PCB 设计、原理图编辑、封装/符号库管理、工程操作等场景。
  既支持通过 AI 直接在 EasyEDA 中在线调试，也支持 EasyEDA 扩展开发。本技能提供完整的
  API 参考（120+ 个类、62 个枚举、70 个接口）、扩展开发文档，以及一个基于 Python MCP
  的桥接服务，可在运行中的 EasyEDA 专业版客户端里执行代码。
  触发词：“嘉立创EDA，启动！”、“立创EDA，启动！”、“EDA，启动！”、
  “EasyEDA”、“PCB”、“原理图”、“封装”、“EDA”、“电路板”、“嘉立创EDA”、“PCB设计”。
  **重要**：嘉立创EDA 的英文名是 **EasyEDA**，两者是同一个产品。
  英文一律写作 “EasyEDA”（除非特指版本号），中文一律写作 “嘉立创EDA”，
  不要使用 “EasyEDA Pro”、“EASYEDA”、“easyeda” 等其它写法。
license: MIT
compatibility: 需要 Python 3.13+、官方 mcp Python SDK（mcp>=2.0）以及支持扩展的 EasyEDA 专业版桌面客户端
metadata:
  author: JLCEDA
  version: "2.0.0"
  openclaw:
    requires:
      bins:
        - python
---

# 嘉立创EDA 专业版 API 技能（Python MCP 版）

通过 AI 以编程方式控制嘉立创EDA 专业版。本技能提供：

1. **完整的 API 参考** —— 120 个类、62 个枚举、70 个接口、19 个类型别名
2. **MCP 工具链（桥接服务）** —— 在运行中的 EasyEDA 专业版客户端里执行代码
3. **代码范式** —— PCB、原理图、库、工程管理的常用操作写法
4. **文档源格式参考** —— 当需要直接分析或修改 EasyEDA 工程源文件、而不走 API 时使用

## 架构

```
┌───────────┐    MCP(stdio)   ┌──────────────────┐   WebSocket    ┌───────────┐
│  AI 智能体 │ ◄────────────► │  easyeda_mcp     │ ◄────────────► │ EasyEDA   │
│ (MCP 客户端)│                │ (Python MCP 服务) │  49620-49629   │ (客户端)  │
└───────────┘                └──────────────────┘                └───────────┘
```

同一个 Python 进程同时承担两件事：

- **MCP 服务**：向 AI 暴露 `eda_*` 工具（默认 stdio 传输）
- **桥接服务**：在 `127.0.0.1:49620-49629` 的某个端口上监听 HTTP + WebSocket，
  EasyEDA 扩展通过 `eda.sys_WebSocket.register()` 连接进来

两端都通过扫描端口范围并校验握手标识 `service: "easyeda-bridge"` 来互相发现。

## 快速开始

### 1. 命令触发

当用户说 **“嘉立创EDA，启动！”**、**“立创EDA，启动！”** 或 **“EDA，启动！”** 时：

**重要：会话初始化** —— 立刻回复下面这段文字，以便设置正确的会话标题：

```
📋 EasyEDA Session
```

然后按下面的步骤继续。

### 2. 安装依赖（仅首次）

```bash
D:/env/eda/python.exe -m pip install -r D:/eda/requirements.txt
```

### 3. 在 MCP 客户端中注册本服务

本服务由 MCP 客户端以子进程方式拉起，通常不需要手动启动。

> ⚠️ **`command` 必须是可执行文件路径**，不能写 `cd D:/eda && python -m easyeda_mcp`
> 这类 shell 组合。MCP 客户端不经过 shell 解释，会把整串内容当成文件名，
> 报 `exec: "...": file does not exist`。

**方式 A（推荐）：安装成本地包后直接用生成的 exe**

```bash
D:/env/eda/python.exe -m pip install --no-build-isolation --no-deps -e D:/eda
```

```toml
[[plugins]]
name    = "easyeda_mcp"
type    = "stdio"
command = "D:/env/eda/Scripts/easyeda-mcp.exe"
args    = []
```

**方式 B：用不依赖工作目录的启动脚本（无需安装）**

```toml
[[plugins]]
name    = "easyeda_mcp"
type    = "stdio"
command = "D:/env/eda/python.exe"
args    = ["D:/eda/scripts/run_mcp.py"]
```

**方式 C：`python -m easyeda_mcp` + 显式工作目录（需客户端支持 `cwd`）**

```json
{
  "mcpServers": {
    "easyeda": {
      "command": "D:/env/eda/python.exe",
      "args": ["-m", "easyeda_mcp"],
      "cwd": "D:/eda"
    }
  }
}
```

> `cwd` 必须是本项目的根目录（包含 `easyeda_mcp/`、`references/` 的那一层），
> 否则 `python -m easyeda_mcp` 找不到包。

> 💡 在 TOML 里写 Windows 路径请用正斜杠（`D:/env/eda/...`）或双反斜杠
> （`D:\\env\\eda\\...`）；单反斜杠如 `\e` 是非法转义，会让配置解析失败。

需要手动启动（例如在终端里观察日志）时：

```bash
cd D:/eda
D:/env/eda/python.exe -m easyeda_mcp
```

> ⚠️ **注意**：该进程的 stdout 是 MCP 协议通道，所有日志都走 stderr；
> 手动运行时不要往 stdout 打印任何内容，否则会破坏 MCP 协议。

也可以改用 HTTP 传输或只跑桥接服务：

```bash
# Streamable HTTP 方式（端点 http://127.0.0.1:8848/mcp）
D:/env/eda/python.exe -m easyeda_mcp --transport streamable-http --http-port 8848

# 只运行桥接服务（配合 curl 调试）
D:/env/eda/python.exe -m easyeda_mcp --transport none

# 连接到已经在运行的桥接服务
D:/env/eda/python.exe -m easyeda_mcp --bridge off
```

### 4. 连接 EasyEDA

在 EasyEDA 专业版中安装 `run-api-gateway.eext` 扩展，下载地址：

- https://jlc-ext.com/item/oshwhub/run-api-gateway

扩展加载后会自动建立 WebSocket 连接。

### 5. 验证连接并选择 EDA 窗口

调用工具 **`eda_status`**：

```json
{
  "bridge": { "running": true, "baseUrl": "http://127.0.0.1:49620", "port": 49620 },
  "eda": {
    "connected": true,
    "windowCount": 1,
    "activeWindowId": "abc-123",
    "windows": [{ "windowId": "abc-123", "connected": true, "active": true }]
  }
}
```

**根据窗口数量处理**：

- **0 个窗口**：告诉用户“当前没有已连接的 EasyEDA 窗口，请确认已安装并加载
  run-api-gateway.eext 扩展。下载：https://jlc-ext.com/item/oshwhub/run-api-gateway”
- **1 个窗口**：会被自动选为活动窗口。告诉用户：“✅ 已连接！当前活动 EDA 窗口：abc-123，可以开始工作了。”
- **2 个及以上窗口**：列出窗口并让用户选择：

  ```
  检测到多个 EasyEDA 窗口：
  - abc-123（活动）
  - def-456

  请问使用哪个窗口？
  ```

用户确认后调用 **`eda_select_window`**（参数 `window_id`），再回复：
“✅ 当前活动 EDA 窗口：abc-123，可以开始工作了。”

### 6. 在 EasyEDA 中执行代码

调用工具 **`eda_execute`**：

```json
{ "code": "return await eda.dmt_Project.getCurrentProjectInfo();" }
```

返回：

```json
{ "ok": true, "windowId": "abc-123", "result": { "uuid": "...", "name": "..." }, "durationMs": 42.7 }
```

## MCP 工具一览

| 工具 | 作用 | 关键参数 |
|------|------|----------|
| `eda_status` | 桥接服务 + EasyEDA 连接状态 | — |
| `eda_list_windows` | 列出已连接的 EasyEDA 窗口 | — |
| `eda_select_window` | 设置默认目标窗口 | `window_id` |
| `eda_execute` | 在 EasyEDA 中执行 JavaScript | `code`，可选 `window_id`、`timeout_seconds` |
| `eda_execute_batch` | 一次调用按顺序执行多段代码 | `steps`（`[{"name", "code"}]`）、`stop_on_error` |
| `eda_ping` | 探测窗口是否在线并测量往返延迟 | 可选 `window_id` |
| `eda_api_search` | 检索 API 文档（类名/方法名/关键字） | `query`、`limit` |
| `eda_api_doc` | 读取某个文档文件 | `path`，可选 `offset`、`limit` |
| `eda_pcb_autoroute` | 分批自动布线（断点续跑） | `nets`、`chunk_size`、`layers`、`optimization` |
| `eda_pcb_drc` | DRC 实时查看（事件缓存 + 增量 diff） | `action`、`mode`、`auto`、`ttl_seconds` |
| `eda_pcb_channel_map` | 通道热力图 + 各网络可达性 | `gx_count`、`gy_count` |
| `eda_pcb_blockers` | 反事实找出挡通道的器件 | `net`、`radius` |
| `eda_pcb_move_reroute` | 事务化移动 + 重布 + DRC 裁判 + 回滚 | `des`、`x`、`y`、`rotation` |
| `eda_pcb_circuit_sim` | 电路仿真（解析式 + MNA） | 无（自动读当前板） |

此外还有三个 MCP 资源，可直接读取：

- `easyeda://skill` —— 本文件
- `easyeda://references/index` —— API 总索引
- `easyeda://references/quick-reference` —— 全部方法签名速查表

## 工具详解

### `eda_execute`

在 EasyEDA 里运行代码并取回返回值。

- `code`：要执行的 JavaScript 片段，以 `async function(eda) { ... }` 形式运行
- `window_id`：可选，指定目标窗口；不传则用当前活动窗口
- `timeout_seconds`：可选，默认 30 秒

成功时返回 `{"ok": true, "windowId": ..., "result": ..., "durationMs": ...}`；
失败时返回 `{"ok": false, "error": ..., "errorType": ...}`，
其中 `errorType` 可能是 `EdaNotConnectedError`（没有窗口/窗口已断开）、
`EdaTimeoutError`（超时）或 `BridgeError`（EasyEDA 内部报错）。

### `eda_execute_batch`

一次往返执行多段代码，适合“先查文档状态、再建图元、最后校验”这类多步操作：

```json
{
  "steps": [
    { "name": "检查工程", "code": "return await eda.dmt_Project.getCurrentProjectInfo();" },
    { "name": "检查文档", "code": "return await eda.dmt_SelectControl.getCurrentDocumentInfo();" }
  ]
}
```

返回值包含 `executed`、`failed`、`aborted` 以及每一步的 `results`。

### `eda_api_search` / `eda_api_doc`

- `eda_api_search("getCurrentProjectInfo")` 返回命中的文件与行号
- `eda_api_doc("references/classes/DMT_Project.md")` 返回带行号的完整内容，
  长文件用 `offset` / `limit` 翻页

只能读取 `references/`、`format/`、`guide/`、`user-guide/` 下的文件。

## PCB 工程工具（6 个）

### `eda_pcb_drc` —— 改完立刻看 DRC

EasyEDA 4.1.78 没有 v4.2 才有的 `startRealTimeDrc()`（恒返回 false），而
`pcb_Drc.check()` 单次 3~8 秒。本工具在 EDA 扩展里注册图元事件监听器，把结果缓存在
`globalThis` 中，改完再调用只在"有变更/缓存过期"时才真正 check。

```json
{"action": "status"}
```

| 参数 | 取值 | 说明 |
|------|------|------|
| `action` | `status`（默认）/`check`/`watch`/`unwatch`/`diff`/`capabilities` | `status` 智能返回缓存；`check` 强制重跑；`watch` 注册监听；`diff` 对比上次快照 |
| `mode` | `auto`（默认）/`realtime`/`poll` | `auto` 在 EDA ≥4.2 走原生实时 DRC，低版本降级 |
| `auto` | `true`/`false` | 仅 `watch` 有效：变更后 1.5 秒自动重跑 |
| `ttl_seconds` | 数字 | 缓存有效期，默认 60 秒 |
| `clearBeforeCheck` | `true`/`false` | `check` 前先清空旧结果，避免残留 |

返回 `{ok, action, mode, stale, total, byGroup, cachedAt, items}`；`diff` 额外返回
`added` / `resolved`。

典型用法：

```text
1. eda_pcb_drc({"action": "watch", "auto": true})   # 会话开始挂上
2. 改线/移件/重建覆铜……
3. eda_pcb_drc({"action": "status"})                # 秒回，不用等 check
4. eda_pcb_drc({"action": "diff"})                  # 看刚才的改动新增/修复了什么
```

注意：缓存只在该 EDA 窗口的 `globalThis` 里，页面刷新会丢，需重新 `watch`。

### `eda_pcb_channel_map` —— 通道热力图 + 网络可达性

两套判定严格分离，别混用：

- **电气连通**：精确几何并查集（点-矩形/点-圆/线段-线段距离 ≤ 0.5 mil），
  回答"网络是否已连、删线后是否断开"；
- **空间通道**：栅格 flood fill（4/8 mil、位掩码、8 邻接），
  回答"两点之间有没有物理通道"。

```json
{"gx_count": 24, "gy_count": 16}
```

返回：

- `nets`：每个网络名及其 `connected`（电气）与 `reachable`（空间）；
- `heatmap`：`gy_count × gx_count` 的 0~100 占用率矩阵；
- `stats`：总数、连通数、不可达数。

用途：布线前看哪里堵、布不通时判断是"没有通道"还是"网络确实断了"。

### `eda_pcb_blockers` —— 移开谁就能通

反事实分析：临时把候选器件从几何里去掉，看该网络的空间通道是否从"不可达"变"可达"。

```json
{"net": "HDMI_RX_5V", "radius": 200}
```

返回 `{ok, net, blocked, candidates, blockers}`，`blockers` 是"移开它就能腾出通道"的器件位号。

候选按**包围盒相交**筛选（网络焊盘 bbox 外扩 100 mil），不按中心距离——J1 这类大连接器
移到通道中间时中心可能很远，但包围盒横跨整条通道，必须被纳入。

使用建议：

```text
1. channel_map 发现某网络 reachable=false
2. blockers(net) 得到候选器件
3. move_reroute 把它们挪开（先删接触线、再移动、DRC 裁判）
4. 重布后 channel_map 复核
```

### `eda_pcb_move_reroute` —— 事务化移动

```json
{"des": "C1", "x": -557.12, "y": -55.32, "rotation": 0}
```

流程：

1. 快照：位置 + **与焊盘几何接触（≤1 mil）的线段**；
2. 只删这些接触线（不是整个网络！C1 挂在 GND 上，按网络删会删掉全板 340+ 段）；
3. 移动器件；
4. 对相关网络 `autoRouting`；
5. 跑 DRC 与基线对比；
6. 变差则位置和走线一起回滚，并复验 DRC 是否回到基线。

返回 `{ok, des, from, to, deletedOldLines, reroute, drcBefore, drcAfter, rolledBack, rollbackVerified}`。

判据：`drcAfter <= drcBefore` 才算成功；`rolledBack=true` 时看 `rollbackVerified` 是否也 true。

### `eda_pcb_circuit_sim` —— 电路仿真

无参数，自动读取当前 PCB 的器件 Value/额定电压和焊盘网络。返回两部分：

1. **解析式检查**（`checks`）：电源树设定点（`0.6×(1+Rtop/Rbot)`）、buck 纹波
   `ΔV=ΔIL/(8·fsw·C)`、PDN 阻抗（C+ESR+ESL 并联扫频）、IR drop、元件降额
   （电容 ≤80%、电阻 ≤50%、电感 ≤70%）；
2. **MNA 全板仿真**（`mna`）：从焊盘/网络自动建网表，
   - `dc`：各电源轨工作点；
   - `rails[rail].zOhm`：逐轨 AC 阻抗（100Hz~10MHz 六个频点）；
   - `rails[rail].tran`：逐轨上电瞬态（`t0`/`tEnd`/`min`/`max`/`t90_us`）。

`mna.py` 支持 R/L/C、独立 V/I 源、受控源、二极管（牛顿迭代），分析类型覆盖 DC/瞬态/AC。
纯 Python 实现，不依赖 numpy/scipy。

解释结果时的经验值：

| 项目 | 合格线 |
|------|--------|
| 电源设定点 | 标称 ±5% |
| buck 输出纹波 | ≤30 mV |
| PDN 阻抗（100k~10M） | ≤0.1 Ω |
| IR drop | ≤3% |
| 降额 | 电容电压≤80%、电阻功率≤50%、电感电流≤70% |

## API 文档

完整的 API 参考在 [references/](references/) 目录：

- [references/_index.md](references/_index.md) —— 全部类、枚举、接口、类型的总索引
- [references/_quick-reference.md](references/_quick-reference.md) —— 全部方法签名速查
- `references/classes/` —— 120 个类文档（`DMT_*`、`PCB_*`、`SCH_*`、`LIB_*`、`SYS_*`、`IPCB_*`、`ISCH_*`）
- `references/enums/` —— 62 个枚举文档
- `references/interfaces/` —— 70 个接口文档
- `references/types/` —— 19 个类型别名文档

### 如何查 API

1. **先看 `_index.md`**，找到任务对应的类或模块
2. **再读类文档**（例如 `references/classes/DMT_Board.md`），确认方法签名
3. **用 `_quick-reference.md`** 快速比对方法签名
4. **查枚举/接口文档**，确认参数类型与返回类型

### 文档源格式文档

如果用户需要直接分析或修改 EasyEDA 工程源文件、而不是通过 API，请使用
[format/](format/) 目录：

- [format/index.md](format/index.md) —— 文档源格式参考总览与版本说明
- `format/project/` —— 工程源结构、元数据、blob、变体、分组数据
- `format/schematic/` —— 原理图源格式：结构、导线、图形、引脚、元件、表格
- `format/pcb/` —— PCB 源格式：图元、焊盘与过孔、图形、文本、属性、规则、拼板数据

当任务涉及理解源文件布局、生成兼容的文档源、或修改后再导入 EasyEDA 时使用这些文件。

### API 模块概览

| 前缀 | 领域 | 主要类 |
|------|------|--------|
| `DMT_` | 文档管理 | Board、EditorControl、Folder、Panel、Pcb、Project、Schematic、SelectControl、Team、Workspace |
| `PCB_` | PCB 与封装 | Document、Drc、Event、Layer、Net、Primitive、PrimitiveComponent、PrimitiveLine、PrimitivePad、PrimitivePour、PrimitiveVia、SelectControl |
| `SCH_` | 原理图 | Document、Event、Primitive、PrimitiveComponent、PrimitiveWire、SelectControl |
| `LIB_` | 库 | 3DModel、Cbb、Classification、Device、Footprint、LibrariesList、PanelLibrary、SelectControl、Symbol |
| `SYS_` | 系统 | Dialog、Environment、FileManager、FileSystem、FontManager、HeaderMenu、I18n、IFrame、LoadingAndProgressBar、Log、Message、MessageBox、MessageBus、PanelControl、Setting、ShortcutKey、Storage、Timer、ToastMessage、WebSocket、Window |
| `IPCB_` | PCB 接口（图元） | PrimitiveArc、PrimitiveComponent、PrimitivePad、PrimitiveFill、PrimitivePour、PrimitiveRegion、PrimitiveVia 等 |
| `ISCH_` | 原理图接口（图元） | PrimitiveArc、PrimitiveComponent、PrimitiveWire、PrimitiveText、PrimitiveRectangle 等 |
| `EPCB_` / `ESCH_` | 枚举 | PrimitiveType、Layer、PadType 等 |

## 代码执行上下文

所有代码都在 EasyEDA 专业版的浏览器运行时里以如下形式执行：

```javascript
async function(eda) {
  // 你的代码写在这里 —— `eda` 是全局 EDA API 对象
  // 必须用 `return` 把结果送回来
  // 不要给生成的代码加注释，因为代码通常以单行形式执行
}
```

**关键规则：**

- `eda` 对象可以访问全部 API 模块（例如 `eda.dmt_Board`、`eda.pcb_Primitive`）。
- 始终以 API 文档为准，不要凭记忆调用。
- 不要给生成的代码加注释，因为代码通常以单行形式执行。
- 必须用 `return` 取回结果 —— `console.log` 的输出不会被捕获。
- 所有返回 Promise 的 API 方法都必须 `await`。
- 代码在浏览器环境运行，不能使用 Node.js 的 API（fs、path 等）。
- 需要给用户可见提示时使用 `eda.sys_Message.showToastMessage(msg)`。
- 遇到枚举值时不要猜测：必须使用枚举成员，例如 PCB 图元创建时的图层参数要写
  `EPCB_LayerId.TOP`，而不是 `1`。
  **注意**：通过 Run API Gateway 执行时枚举符号不在作用域（详见文末「15. 桥接层修复」第 6 条），
  `eda_execute` 会自动为用到的枚举注入 `const` 定义；若改用裸数字，请先用
  `eda_enum(name="EPCB_LayerId")` 查到准确取值。

### 扩展运行环境的限制

编写 EasyEDA 扩展时，主进程**禁止**使用标准浏览器 API，请改用 EDA 提供的替代方案：

| 用途 | ❌ 禁止 | ✅ 请改用 |
|------|---------|-----------|
| 获取用户输入 | — | `eda.sys_Dialog.showInputDialog()` |
| 用户选择 | — | `eda.sys_Dialog.showSelectDialog()` |
| 显示消息 | `alert()` | `eda.sys_Dialog.showInformationMessage()` |
| 确认操作 | `confirm()` | `eda.sys_Dialog.showConfirmationMessage()` |
| Toast 提示 | 直接操作 DOM | `eda.sys_Message.showToastMessage()` |
| 存储数据 | `localStorage`（主进程） | `eda.sys_Storage.setExtensionUserConfig(key, value)` |
| 自定义界面 | 操作宿主 DOM | `eda.sys_IFrame.openIFrame()` |
| 展示 HTML | `showInformationMessage(html)` | 必须使用 iframe |
| 打开链接 | `window.open()` | `eda.sys_Window.open()` |
| 浏览器硬件 API | 主进程中使用 | 在 iframe 中可用（`navigator.serial` 等） |

**说明**：`localStorage`、`window`、`document` 等对象在 `sys_IFrame` **内部**可用，
但在扩展主进程中不可用。

### 扩展 ↔ IFrame 的数据传递

扩展主进程与 `sys_IFrame` iframe 是**相互隔离的上下文**，传递数据有两种方式：

**方式 A（推荐）：用 `eda.sys_Storage` 作为桥梁**

```javascript
// 扩展主进程中：
await eda.sys_Storage.setExtensionUserConfig('myKey', JSON.stringify(data));

// iframe 中：
const data = JSON.parse(await eda.sys_Storage.getExtensionUserConfig('myKey'));
```

**方式 B：两侧都可以直接访问 `eda`，从任意一侧调用同一个 API**

```javascript
// iframe 的 HTML 中（不是 window.parent.eda）：
const info = await eda.dmt_Project.getCurrentProjectInfo();
```

## 通信协议

> 这一节描述的是 EasyEDA 扩展与桥接服务之间的底层契约。使用 MCP 工具时无需关心，
> 只有在自己编写客户端（例如用 curl 或自研脚本调试）时才需要了解。

### 端口发现

桥接服务监听 **49620-49629** 中第一个可用端口。扫描端口并校验服务标识即可找到它：

```bash
for port in $(seq 49620 49629); do
  resp=$(curl -s http://127.0.0.1:$port/health 2>/dev/null)
  if echo "$resp" | grep -q '"easyeda-bridge"'; then
    BRIDGE_PORT=$port; break
  fi
done
```

### 握手

- **HTTP**：`GET /health` 返回 `{ "service": "easyeda-bridge", "edaConnected": bool, ... }`
- **WebSocket**：连接建立后服务端立即发送 `{ "type": "handshake", "service": "easyeda-bridge" }`
- 客户端在使用连接前**必须**校验 `service === "easyeda-bridge"`

### 报文格式

WebSocket / HTTP 上的 JSON 报文：

| 字段 | 类型 | 说明 |
|------|------|------|
| `type` | `"execute"` \| `"result"` \| `"error"` \| `"ping"` \| `"pong"` \| `"handshake"` \| `"register"` | 报文类型 |
| `id` | `string` | 请求 UUID，用于匹配请求与响应 |
| `code` | `string` | 要执行的 JavaScript（`execute` 报文） |
| `result` | `any` | 执行结果（`result` 报文） |
| `error` | `string` | 错误信息（`error` 报文） |
| `service` | `string` | 服务标识（`handshake` 报文） |
| `windowId` | `string` | EDA 窗口 ID（`register` 报文与 `execute` 报文） |
| `timestamp` | `number` | Unix 毫秒时间戳 |

### HTTP 接口

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/health` | 健康检查与连接状态 |
| `GET` | `/eda-windows` | 列出已连接的 EDA 窗口 |
| `POST` | `/eda-windows/select` | 选择活动窗口，body：`{"windowId": "..."}` |
| `POST` | `/execute` | 执行代码，body：`{"code": "...", "windowId": "可选"}` |

```bash
# 健康检查
curl http://127.0.0.1:49620/health

# 执行代码
curl -X POST http://127.0.0.1:49620/execute \
  -H "Content-Type: application/json" \
  -d '{"code": "return await eda.dmt_Project.getCurrentProjectInfo();"}'
```

## 常用代码范式

### 工程与板子操作

```javascript
// 获取当前工程信息
return await eda.dmt_Project.getCurrentProjectInfo();

// 列出工程中的全部板子
return await eda.dmt_Board.getAllBoardsInfo();

// 新建板子（可选关联原理图/PCB）
return await eda.dmt_Board.createBoard();

// 切换到某个文档页签
await eda.dmt_EditorControl.activateDocument(tabId);
```

### 示例：按名称打开工程并打开它的第一个原理图页

```javascript
const targetProjectName = 'NE555 Circuit';

// 1. 按团队/文件夹枚举工程，按名称找到目标工程
const teams = await eda.dmt_Team.getAllTeamsInfo();
const seenProjectUuid = new Set();
let targetProjectUuid = null;

for (const team of teams || []) {
  const teamProjects = await eda.dmt_Project.getAllProjectsUuid(team.uuid);
  for (const projectUuid of teamProjects || []) {
    if (seenProjectUuid.has(projectUuid)) continue;
    seenProjectUuid.add(projectUuid);

    const info = await eda.dmt_Project.getProjectInfo(projectUuid);
    const name = info?.friendlyName || info?.name || '';
    if (name === targetProjectName) {
      targetProjectUuid = projectUuid;
      break;
    }
  }
  if (targetProjectUuid) break;

  const folderUuids = await eda.dmt_Folder.getAllFoldersUuid(team.uuid);
  for (const folderUuid of folderUuids || []) {
    const folderProjects = await eda.dmt_Project.getAllProjectsUuid(team.uuid, folderUuid);
    for (const projectUuid of folderProjects || []) {
      if (seenProjectUuid.has(projectUuid)) continue;
      seenProjectUuid.add(projectUuid);

      const info = await eda.dmt_Project.getProjectInfo(projectUuid);
      const name = info?.friendlyName || info?.name || '';
      if (name === targetProjectName) {
        targetProjectUuid = projectUuid;
        break;
      }
    }
    if (targetProjectUuid) break;
  }
  if (targetProjectUuid) break;
}

if (!targetProjectUuid) {
  throw new Error(`Project not found: ${targetProjectName}`);
}

// 2. 打开工程
const opened = await eda.dmt_Project.openProject(targetProjectUuid);
if (!opened) {
  throw new Error(`Failed to open project: ${targetProjectName}`);
}

// 3. 取第一个原理图页并打开
const schematics = await eda.dmt_Schematic.getAllSchematicsInfo();
if (!Array.isArray(schematics) || schematics.length === 0) {
  throw new Error(`No schematics found in project: ${targetProjectName}`);
}

const firstSchematic = schematics[0];
const pages = await eda.dmt_Schematic.getAllSchematicPagesInfo();
const firstPage = (pages || []).find((page) => page.parentSchematicUuid === firstSchematic.uuid)
  || firstSchematic.page?.[0];

if (!firstPage) {
  throw new Error(`No schematic page found in: ${firstSchematic.name}`);
}

const tabId = await eda.dmt_EditorControl.openDocument(firstPage.uuid);

return {
  projectUuid: targetProjectUuid,
  schematicUuid: firstSchematic.uuid,
  pageUuid: firstPage.uuid,
  tabId,
};
```

**注意：**

- `dmt_Project.getAllProjectsUuid()` 实际上并不是一个无需参数的全局枚举接口。
  要可靠地按名称查找工程，必须先遍历团队、再遍历文件夹。
- `openProject(projectUuid)` 可能会丢弃当前已打开工程中未保存的修改，调用前请谨慎。

### PCB 图元操作

```javascript
// 获取选中的图元
const selected = eda.pcb_SelectControl.getAllSelectedPrimitives_PrimitiveId();

// 在铜层上画一条线
// 图层参数使用 EPCB_LayerId 枚举 —— 见 references/enums/EPCB_LayerId.md
await eda.pcb_PrimitiveLine.create(
  "GND",                       // net（字符串）
  EPCB_LayerId.TOP,            // 图层 —— 用枚举，不要写裸数字
  0,                           // startX（单位：1mil）
  0,                           // startY（单位：1mil）
  1000,                        // endX（单位：1mil）
  0,                           // endY（单位：1mil）
  10,                          // lineWidth
  false                        // primitiveLock
);

// 修改已有图元（异步范式）
const prim = await eda.pcb_PrimitiveComponent.get([id]);
const asyncPrim = prim.toAsync();
asyncPrim.setState_X(newX);
asyncPrim.setState_Y(newY);
asyncPrim.done();
```

### PCB 图元异步范式（重要）

**修改** PCB/SCH 图元时必须使用异步范式：

```javascript
// 1. 取图元
const prim = await eda.pcb_PrimitiveVia.get([viaId]);
// 2. 转换为异步模式
const asyncPrim = prim.toAsync();
// 3. 设置新值
asyncPrim.setState_X(newX);
asyncPrim.setState_Y(newY);
asyncPrim.setState_Diameter(diameter);
// 4. 提交修改
asyncPrim.done();
```

### 原理图操作

```javascript
// 获取全部页面
return await eda.dmt_Schematic.getAllSchematicDocumentsInfo();

// 创建原理图元件
// component 参数需要 {libraryUuid, uuid} 对象 —— 不是普通字符串
await eda.sch_PrimitiveComponent.create(
  { libraryUuid: "...", uuid: "device-uuid-from-library" },  // 元件对象
  5000,                        // x（单位：0.01inch = 10mil）
  5000,                        // y（单位：0.01inch = 10mil）
  "",                          // subPartName
  0,                           // rotation（角度，普通数字）
  false,                       // mirror
  true,                        // addIntoBom
  true                         // addIntoPcb
);

// 获取选中的原理图图元
const ids = eda.sch_SelectControl.getAllSelectedPrimitives_PrimitiveId();
```

### 库操作

```javascript
// 列出工作区中的全部库
return await eda.lib_LibrariesList.getAllLibrariesList();

// 搜索器件
return await eda.lib_Device.search("STM32");

// 获取符号信息
return await eda.lib_Symbol.get(symbolUuid);
```

### 系统函数

```javascript
// 显示 toast 提示
eda.sys_Message.showToastMessage("操作完成！");

// 显示确认对话框
const confirmed = await eda.sys_Dialog.showConfirmationMessage("继续吗？");

// 文件系统（仅限扩展沙箱）
const content = await eda.sys_FileSystem.readFileFromFileSystem(path);
await eda.sys_FileSystem.saveFileToFileSystem(path, content);
```

### DRC（设计规则检查）

```javascript
// 运行 DRC 检查
const passed = await eda.pcb_Drc.check(true, true, false);
// passed 是布尔值：true 表示通过，false 表示存在错误
```

## 常见错误

### 仔细核对 API 签名（关键）

**调用任何 API 方法之前，必须先完整阅读 `references/` 下的签名 —— 包括参数类型、
返回类型和备注。**

AI 智能体常常因为只扫一眼文档就犯下面这些错误：

**错误 1：没有 `await` 返回 Promise 的方法**

几乎所有 EDA API 方法都返回 `Promise<T>`。忘记 `await` 得到的是 Promise 对象，
而不是真正的结果。

```javascript
// 错误：缺少 await —— 拿到的是 Promise，不是工程信息
const project = eda.dmt_Project.getCurrentProjectInfo();
console.log(project); // Promise { <pending> }

// 正确：异步方法一律 await
const project = await eda.dmt_Project.getCurrentProjectInfo();
console.log(project); // { uuid: "...", name: "...", ... }
```

**如何判断是否需要 `await`**：看签名中的返回类型。写的是 `Promise<...>` 就必须 `await`。

```typescript
// 摘自 references/classes/DMT_Project.md：
getCurrentProjectInfo(): Promise<IDMT_ProjectItem | null>
//                       ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
//                       这是 Promise —— 必须 await
```

**错误 2：用裸数字代替枚举值**

很多参数要求特定的枚举值，用错数字会静默产生错误结果。

```javascript
// 错误：图层 "1" 到底指什么？哪一层铜？信号层还是阻焊层？
await eda.pcb_PrimitiveLine.create("GND", 1, 0, 0, 1000, 0, 10, false);

// 正确：用枚举 —— EPCB_LayerId.TOP 含义明确且类型安全
// 具体值见 references/enums/EPCB_LayerId.md
await eda.pcb_PrimitiveLine.create("GND", EPCB_LayerId.TOP, 0, 0, 1000, 0, 10, false);
```

> ⚠️ 但要注意：通过 **Run API Gateway** 执行时，枚举符号**不在作用域**（`EPCB_LayerId`
> 会抛 ReferenceError，实测确认）。`eda_execute` 会自动注入代码里用到的枚举定义，
> 所以上面的写法仍然可用；若关掉了注入（`inject_enums=false`），就必须先
> `eda_enum(name="EPCB_LayerId")` 查值、再写裸数字。

**使用数字常量前，永远先去 `references/enums/` 查枚举（或直接调 `eda_enum`）。**

**错误 3：凭感觉假设参数类型**

不同 API 的单位不同、ID 格式不同、可选参数约定也不同，绝不能假设。

```javascript
// 错误：假设 create() 的参数顺序/类型与 modify() 相同
// create() 和 modify() 的参数顺序与类型经常不同

// 正确：为你实际调用的那个方法读准确签名
// 见 references/classes/<ClassName>.md 中该方法的签名
```

**每次调用 API 之前的小结：**

1. **读签名** —— 参数类型、返回类型，都在 `references/classes/`
2. **是 Promise 就 `await`** —— 检查返回类型是否为 `Promise<...>`
3. **用枚举** —— 去 `references/enums/` 查，不要猜数字或字符串
4. **看备注** —— “备注”一节往往有最关键的使用说明

### 坐标单位（关键）

**不同领域的坐标单位不同：**

| 领域 | 单位 | 换算 |
|------|------|------|
| **PCB** | 1mil | 1mm ≈ 39.37 个单位 |
| **原理图** | 0.01inch（10mil） | 1mm ≈ 3.937 个单位 |

**这是 AI 智能体最容易犯的错误。** 单位搞混会把元件放到错误位置。

- PCB：1 个单位 = 1mil = 0.001 英寸 = 0.0254 mm
- 原理图：1 个单位 = 0.01 英寸 = 10mil = 0.254 mm

单位用错，元件会放到目标位置的 **10 倍远处**。

```javascript
// 错误：以为单位是 1mil（实际是 0.01inch = 10mil）
// 想放在 x=500（以为 500mil = 0.5inch），实际 500 个单位 = 5000mil = 5inch！
await eda.sch_PrimitiveComponent.create({libraryUuid: "...", uuid: "dev-uuid"}, 500, 0, "", 0, false, true, true);

// 正确：50 个单位 = 500mil = 0.5inch（以 0.01inch 为单位）
await eda.sch_PrimitiveComponent.create({libraryUuid: "...", uuid: "dev-uuid"}, 50, 0, "", 0, false, true, true);
```

### 文档状态（关键）

**新建工程之后，必须先打开该工程，才能操作其中的文档。**

操作文档前始终确认：

1. **工程已打开** —— 用 `eda.dmt_Project.getCurrentProjectInfo()` 确认
2. **当前文档正确** —— 用 `eda.dmt_SelectControl.getCurrentDocumentInfo()` 检查文档类型
3. **文档类型与 API 领域匹配** —— PCB API 需要活动的 PCB 文档，SCH API 需要活动的原理图文档

```javascript
// 错误：不检查文档状态就直接操作
await eda.pcb_PrimitiveLine.create(...); // 没有打开 PCB 文档时可能失败

// 正确：先确认文档状态
const project = await eda.dmt_Project.getCurrentProjectInfo();
if (!project) {
  // 如果刚用 createProject() 新建了工程，必须先打开它才能继续操作！
  return "错误：当前没有打开的工程。若刚用 dmt_Project.createProject() 新建工程，必须先调用 dmt_Project.openProject(projectPath) 打开它。工程打开之前无法操作其中的文档。";
}

const doc = await eda.dmt_SelectControl.getCurrentDocumentInfo();
if (doc?.documentType !== EDMT_EditorDocumentType.PCB) {
  return "错误：当前没有激活的 PCB 文档，请先打开一个 PCB 文档。";
}

// 现在才能安全地执行 PCB 操作
await eda.pcb_PrimitiveLine.create(...);
```

**在错误的文档类型上操作会返回错误或 null。** 例如：

- 没有活动的 PCB 文档（`documentType !== EDMT_EditorDocumentType.PCB`）时执行 `PCB_*` API → 报错或返回 null
- 没有活动的原理图文档（`documentType !== EDMT_EditorDocumentType.SCHEMATIC_PAGE`）时执行 `SCH_*` API → 报错或返回 null

### IFrame 上下文 —— 直接使用 `eda`（关键）

编写通过 `sys_IFrame.openIFrame()` 打开 iframe 的扩展代码时，iframe 内 HTML 中的
JavaScript 可以**直接**访问全局 `eda` 对象。**不要**使用 `window.parent.eda`。

**原因：** EasyEDA 会把 `eda` 对象自动注入 iframe 的执行上下文。标准的浏览器跨帧
写法（`window.parent`、`parentWindow` 等）在这里不适用，会直接失败。

```javascript
// 错误：通过 window.parent 访问父窗口的 eda 对象
// 这是熟悉 Web iframe 写法的 AI 智能体常犯的错误
const project = await window.parent.eda.dmt_Project.getCurrentProjectInfo();

// 正确：iframe 上下文中可以直接使用 eda
const project = await eda.dmt_Project.getCurrentProjectInfo();
```

**这条规则适用于 `sys_IFrame` 内的所有代码：**

- 通过 `openIFrame()` 加载的扩展 UI 面板
- 扩展包内任何以 iframe 形式展示的 HTML 文件

**规则**：在 EasyEDA 扩展中，无论代码跑在主上下文还是 `sys_IFrame` 创建的 iframe 内，
访问 API 一律用 `eda.xxx`，绝不要用 `window.parent.eda` 或其它父帧访问方式。

## 多窗口支持

当多个 EasyEDA 窗口连接到桥接服务时，**不必**每次操作都检查窗口选择。只在以下情况检查：

1. **首次进行 EDA 操作** —— 确认当前活动窗口并告知用户
2. **出现 EDA 断开错误** —— 若返回窗口已断开的错误，让用户重新选择窗口

```text
# 列出全部已连接的 EDA 窗口
eda_list_windows()

# 指定某个窗口
eda_select_window(window_id="abc-123-def")

# 在指定窗口上执行
eda_execute(code="return await eda.dmt_Project.getCurrentProjectInfo();", window_id="abc-123-def")
```

只连接了一个 EDA 窗口时，它会被自动选为活动窗口。

## 调试技巧与失败处理

### 排查步骤

1. **先看状态**：调用 `eda_status`，确认 `bridge.running` 与 `eda.connected`
2. **EDA 未连接？** 确认 EasyEDA 中已加载桥接扩展。下载：https://jlc-ext.com/item/oshwhub/run-api-gateway
3. **超时错误**：默认超时 30 秒。若出现超时：
   - 检查是否打开了正确的工程与文档（`dmt_Project.getCurrentProjectInfo()`）
   - 若没有打开的工程，先用 `dmt_Project.openProject(projectPath)` 打开一个
   - 若没有活动文档，用 `dmt_EditorControl.openDocument(docId)` 打开正确类型的文档（PCB/原理图）
   - 若文档类型不对（例如在原理图上调用 PCB API），先切换到正确的文档
   - 复杂操作可以考虑拆分成多段代码（`eda_execute_batch`）
4. **检查返回值**：很多方法失败时返回 `null` —— 始终校验返回值
5. **图层数字**：使用 `references/enums/` 中的枚举（例如 `EPCB_LayerId`）
6. **EDA 窗口断开**：若返回窗口断开的错误，用 `eda_list_windows` 查看可用窗口，
   再用 `eda_select_window` 切换到另一个窗口
7. **权限错误**：所有 API 接口都受 EDA 权限系统控制。如果某个 API 持续失败
   （报错或返回 null），而其它 API 正常，并且你已确认调用方式与文档完全一致，
   那么很可能是**被权限拦截**，而不是代码 bug。EDA 客户端可能基于授权、工程设置
   或文档状态限制某些操作。此时应告知用户该操作可能需要更高权限或不同的 EDA 版本。
8. **持续报错？** 如果已确认调用与文档完全一致、也排除了权限问题，仍然出现异常，
   建议通过嘉立创EDA 官方支持渠道反馈

### 失败处理规则

开发扩展时遵循以下规则：

| 情况 | 处理方式 |
|------|----------|
| API 方法在文档中不存在 | **立刻停止** —— 告知用户该 API 不存在 |
| 读完文档仍不确定签名 | **停止生成** —— 回到查文档步骤重新确认 |
| 发现使用了被禁的浏览器 API | **自动替换** 为 `eda.sys_*` 对应接口 |
| 菜单 ID 冲突 | 加前缀区分（例如 `my-plugin-home`、`my-plugin-sch`） |
| 权限被拦截 | 告知用户 —— 可能需要不同的 EDA 版本或授权 |

**关键：绝不要猜测 API 签名。** 如果 `references/classes/` 里没有记录，那就不能这么用。

## AI 智能体的工作流程

当用户要求执行 EDA 操作时：

1. **理解任务** —— 属于哪个领域？（PCB/SCH/LIB/工程）
2. **确认文档状态** —— 检查 `eda.dmt_Project.getCurrentProjectInfo()` 和
   `eda.dmt_SelectControl.getCurrentDocumentInfo()`
3. **确认文档类型正确** —— PCB 操作需要活动的 PCB 文档，SCH 操作需要活动的原理图文档
4. **查 API** —— 从 `references/` 读取对应的类文档
5. **查类型** —— 读取枚举/接口文档，确认参数类型
6. **写代码** —— 遵守上面的代码执行上下文规则
7. **执行** —— 用 `eda_execute` 发送并检查返回结果
8. **迭代** —— 出现错误时读错误信息并修正

不确定某个 API 时：

- 在 `_quick-reference.md` 中搜索方法名（或用 `eda_api_search`）
- 读取具体的类文档，查看详细签名与备注
- 查看接口文档，了解复杂参数类型

## 会话管理

### 识别话题切换

桥接服务运行时会占用少量系统资源。如果用户**连续 3 次**切换到与
EasyEDA/EDA/PCB/原理图完全无关的话题，可以主动询问是否要关闭桥接服务：

> “我注意到我们已经不在处理 EasyEDA 相关的工作了。桥接服务仍在后台运行，
> 需要我停止它来释放资源吗？”

**不要自动关闭** —— 用户可能稍后还会回到 EDA 工作。只做询问，用户确认后再关闭。

### 关闭桥接服务

本服务通常是 MCP 客户端拉起的子进程，停止方式取决于客户端：

- 退出当前的 MCP 会话（或结束该 MCP 客户端进程）即可释放端口
- 手动启动时，在终端按 `Ctrl+C`
- 需要确认端口占用情况时：

  ```powershell
  # 查看 49620-49629 上的监听进程
  Get-NetTCPConnection -LocalPort 49620..49629 -State Listen | Select-Object LocalPort, OwningProcess
  ```


---

# 实战踩坑记录（AI 实践总结，2026-10）

以下全部是在真实板上做完整 PCB 设计时踩过的坑，是文档之外的补充，建议优先参考。

> **检索方式最重要**：`eda_api_search` 的 query **必须是英文关键词**
> （cli.md 第 204 行原文：*Search keywords are English words such as `session` `invoke`
> `doc` `permission` `BoardOutline`*）。搜中文（「覆铜」「网络标签」）会 0 命中，
> 极易误判成"API 不存在"。命中后用 `eda_api_doc(path="references/classes/XXX.md")` 读完整类文档。
> **特别注意"实例方法"**：很多能力在 `create()` 返回的实例上，而不在类上（见第 1 节）。

## 1. 覆铜（Pour）—— 用 `rebuildCopperRegion()`，这是唯一正确做法

**不要**手工算避让多边形、**不要**改工程文件、**不要**去找什么"覆铜填充体"。
`IPCB_PrimitivePour` **实例**上有 `rebuildCopperRegion()`，会按当前 DRC 规则自动避让并生成填充：

```javascript
const poly = eda.pcb_MathPolygon.createPolygon([-1268,-868,'L',1268,-868,1268,868,-1268,868,-1268,-868]);
const cp   = eda.pcb_MathPolygon.createComplexPolygon([poly]);

const pour = await eda.pcb_PrimitivePour.create('GND', 1, cp);  // 1 = Top, 2 = Bottom, 15/16 = Inner
await pour.rebuildCopperRegion();                                // ★ 自动避让 + 生成填充
await eda.pcb_Document.save();
```

`IPCB_PrimitivePour` 实例上可用方法（`Object.getOwnPropertyNames(proto)` 实测）：

```
getState_/setState_  Net, Layer, ComplexPolygon, PourFillMethod,
                     PreserveSilos, PourName, PourPriority, LineWidth, PrimitiveLock
reset / done / toAsync / toSync / isAsync
getCopperRegion        // 读回填充体
rebuildCopperRegion    // ★ 重建填充（自动避让）
convertToFill          // 转成 Fill 图元
convertToPolyline / convertToRegion
```

`IPCB_PrimitivePoured`（填充体）上还有：
`getState_PourFills()` / `addSolderMaskFill(id)` / `convertToFill(id)` / `deletePourFills(ids)`。

**实测效果**（66×46mm 四层板、71 器件、502 条走线）：

| 做法 | 填充相关 DRC 违规 | GND 连接性错误 |
|---|---|---|
| `rebuildCopperRegion()` | **0** | 107 → **2** |
| 手工注入带 79 个避让孔的 Fill | 18~33（孔半径怎么调都留） | 107 → 0 |

**操作要点**：
1. 覆铜铺 **L1 / L2** 最省事（焊盘就在表面层，不需要过孔）；
2. 只铺 L1 时，约 70 个 GND 焊盘连不上（L1 走线太密，覆铜挤不进去），
   这时给焊盘**打盘中孔接到 L2 覆铜**（L2 走线少），GND 错误 70 → **2**；
3. 焊盘/走线变动后**必须重新 `rebuildCopperRegion()`**，否则填充还是旧的；
4. **不要把 4 个电源网络一次性全铺内层**：VCC3V3 加 35 个盘中孔后过孔到 217，
   虽然它自己少 12 个错，却引出 27 个 `钻孔到钻孔 / 过孔到过孔`，净亏。
   电源平面要配"一簇一孔"（第 6 节）。

## 2. 盘中孔（Via on SMD Pad）需要放宽 DRC 规则

盘中孔是嘉立创支持的工艺（树脂塞孔+电镀），但默认 DRC 会报「贴片焊盘到过孔」。规则表：

```javascript
const cfg = await eda.pcb_Drc.getCurrentRuleConfiguration();
const t = cfg.config.Spacing['Safe Spacing'].copperThickness1oz.tables['1'].content;
// row: [Track, SMD Pad, TH Pad, SMD TestPoint, TH TestPoint, Via, Fill, CopperZone,
//       Slot, Line, Text/Image, Board Outline, Hole]      col: 同上去掉 Hole
t[5][0] = 0;   // Via  ↔ Track
t[5][1] = 0;   // Via  ↔ SMD Pad   ← 盘中孔关键
t[6][0] = 0;   // Fill ↔ Track
t[12][0] = 0;  // Hole ↔ Track
await eda.pcb_Drc.overwriteCurrentRuleConfiguration(cfg.config);
```

**放宽规则只用于工艺确实支持的项**（如盘中孔），不要用宽规则去掩盖真实间距问题。

## 3. `autoRouting` 的行为边界

- 单次可靠处理 **≤10 个简单网络**；20 个以上常返回 `undefined` 并**清空全部走线**；
- 内部参数：`isKeep` 默认 `"keep"`（保留已有布线），`routingEffectPriority` 就是 `optimization`；
- `optimization: 0` 极快但线很碎（25 秒 4500 段），收尾易崩；`optimization: 1` 质量好但慢；
- **显式传 `layers: [...]` 收窄层会显著降低成功率**（少一层更容易无解），不建议传；
- 电源/地类几十上百焊盘的网络**单网络也 `undefined`**，不要指望布线器，用覆铜；
- **覆铜边框会挡布线通道**（`succ: 0`，能跑但布不通）。
  **正确顺序：先布线 → 后铺覆铜。**

## 4. 超时 ≠ 失败

`POST /execute` 支持 body 里的 `timeout`（秒，上限 600），MCP 工具对应 `timeout_seconds`。
超时返回会带：

```json
{"timeout": true, "stillRunning": true, "hint": "...", "note": "这不是失败：EasyEDA 仍在执行"}
```

**看到这个不要重复提交同一任务**——布线类接口每次调用会先清空再用 isKeep 重布，重提等于扔掉成果。
用轻量查询轮询真实进度。

## 5. 性能陷阱：`getAll()` vs `getAllPrimitiveId()`

`pcb_PrimitiveLine.getAll()` 在千条走线时要**几十秒**，会把请求拖过超时。只读计数请用：

```javascript
await eda.pcb_PrimitiveLine.getAllPrimitiveId()            // 毫秒级
await eda.pcb_PrimitivePad.getAllPrimitiveId()
```

注意 `pcb_PrimitiveComponent.getAllPrimitiveId('part')` 对 PCB 文档返回 0（PCB 的 componentType 不是 `'part'`），
需要用 `pcb_PrimitiveComponent.getAll()` 拿列表（会慢但可用）。

## 6. 电源网络：「一簇一孔」而不是每脚一孔

VCC3V3(35) + VCC1V1(24) + VCC2V5(11) + VCC5V(13) = 83 个焊盘。若每个都打盘中孔，
过孔数会到 300+，直接触发 `钻孔到钻孔` / `过孔到过孔` 违规。

正确做法：
1. 同网络焊盘按距离**聚类**（Union-Find，半径 ~200 mil）；
2. 每簇内用**同层短线**把焊盘连起来（同网络不冲突）；
3. 每簇只打 **1 个**过孔（打在簇内某焊盘中心，盘中孔）；
4. 内层覆铜 + `rebuildCopperRegion()`。

这样 83 个焊盘能压到 25~30 个过孔。

## 7. 层 ID（4 层板）

`1` = Top，`2` = Bottom，**`15` / `16` = Inner1 / Inner2**（不是 3/4）。
`11` = Board Outline（`layerType` 实际是 `"OUTLINE"`，但 API 返回 `"OTHER"`）。

## 8. 板框只能用 `Polyline` + `createPolygon`

`pcb_PrimitiveLine.create` **只接受 SIGNAL 层**，传 11 会报「参数不正确」。

```javascript
const poly = eda.pcb_MathPolygon.createPolygon([-1300,-900,'L',1300,-900,1300,900,-1300,900,-1300,-900]);
await eda.pcb_PrimitivePolyline.create('', 11, poly, 10, false);
```

## 9. 网络标签：用 NetPort / NetFlag

`sch_PrimitiveAttribute.createNetLabel(x,y,net)` 存在但**返回 undefined、不创建任何东西**。

```javascript
await eda.sch_PrimitiveComponent.createNetPort('BI', net, x, y);       // 普通信号
await eda.sch_PrimitiveComponent.createNetFlag('Power', net, x, y);    // 电源
await eda.sch_PrimitiveComponent.createNetFlag('Ground', 'GND', x, y); // 地
```

同名自动合并。元件引脚连接方式：从引脚端点拉一小段 wire，再在 wire 端点放 NetPort/NetFlag。

## 10. 已验证失效 / 不可靠的接口

| 接口 | 现象 |
|---|---|
| `pcb_Document.importChanges(uuid)` | 返回 true 但**不导入**（首次新建工程时除外）|
| `dmt_EditorControl.closeDocument(uuid)` | 常返回 false；用 `openDocument` 切走再切回更可靠 |
| `sys_FileManager.setDocumentSource()` | 返回 true 但内容被丢弃 |
| `pcb_PrimitivePoured.create(...)` | 各种参数都返回 `null`（填充由 `rebuildCopperRegion` 生成，不要直接 create）|
| `pcb_Document.autoLayout()` | 可用，71 器件约 45 秒 |

## 11. 不要靠"改工程文件 + 重读"来生效

`.eprj3` 虽然号称双向同步，但实测：

| 操作 | 是否重读磁盘改动 |
|---|---|
| 只改文件 | ❌ |
| `openDocument` 切走再切回 | ❌（只切视图）|
| `openProject(uuid)` | ⚠️ 会重读，但**先把内存状态写盘覆盖你的改动** |
| 重启 EasyEDA | ✅（工程须先未打开）|

**结论：工程开着时改 `.epcb2` 基本无效。能用 API `create()` 的就不要改文件。**
（`FILL` 记录确实有两种形态：带完整 body 的有效记录，和 `...|||` 空 body 占位；
解析时必须先按 body 是否为空过滤，否则会误判成"注入被丢弃"。）

## 12. 间距违规：优先改布局，并且算法要排除同器件

自己算焊盘间距时，**必须排除同器件焊盘对和同网络焊盘对**，否则封装内部引脚（如 SOIC-8 引脚间距
50 mil）会被误判成违规，一次能算出 200 个假阳性。

```javascript
// 器件 id 用 pad.primitiveId.slice(0,16)
// 焊盘尺寸从 pad.pad 取：pad.pad = ["RECT", w, h, rotation]
```

真实案例：18 个违规里有 12 个是同一排 0402 去耦电容间距只有 52 mil（焊盘相触），
把间距改到 90 mil + 整体平移即可全清，**而且不影响已布的信号线**（这些电容连的是电源/地，
本来就没布线）。

## 13. 建议：长任务走异步 job 而不是拉长 timeout

把 timeout 拉到 600 秒仍会阻塞调用方 5~10 分钟。生产可用应改成 job 模式：
`POST /execute` 提交后立刻返回 `jobId`，`GET /job/{id}` 轮询状态与结果。


## 14. 改层数 / 恢复文件后的必做动作

这两条都是实测踩出来的，不做就会得到完全错误的结果：

### 14.1 `setTheNumberOfCopperLayers()` 之后必须重建覆铜

改层数（4→6 或 6→4）会让已有覆铜的填充体失效。实测：

| 操作 | DRC |
|---|---|
| 4 层 + GND@L1/L2（已 rebuild）| 157（GND 连通性错误 **2**）|
| 把层数改成 6（未 rebuild）| 208（GND **53**）|
| 改回 4 层（未 rebuild）| 208（GND **53**）|
| 改回 4 层 + `rebuildCopperRegion()` | **157（GND 2）** ← 恢复 |

### 14.2 从磁盘恢复 `.epcb2` 后也必须重建覆铜

```javascript
// 恢复文件后（openProject 重读），一定要：
for (const p of await eda.pcb_PrimitivePour.getAll()) {
  await p.rebuildCopperRegion();
}
await eda.pcb_Document.save();
```

不 rebuild 的话，填充体还是旧几何，DRC 会显示 GND 错 178 这种"看起来板子坏了"的结果。

### 14.3 关于 6 层板的一个实测反例

把板子改成 6 层（L1/L2 信号 + L15/L16/L17/L18 内层）**并不会自动变好**：

- L15/L16 上原本有 105 条信号走线（4 层时 autoRouting 铺的），改成"内层平面"后
  这些走线会和平面冲突，把平面切碎；
- 要发挥 6 层优势，必须**先把内层走线全部挪到 L1/L2 再重布线**，而 `autoRouting`
  在 `layers: [1,2]` 限制下成功率很低（见第 3 节）；
- 过孔方面：LQFP-100 引脚间距 0.5mm，用 12/24mil（0.3/0.6mm）孔会让相邻孔边缘距
  0.2mm < 0.25mm 规则；改用 8/16mil（0.2/0.4mm）虽满足间距，但**违反最小过孔尺寸**
  （实测报 103 个「过孔尺寸」物理错误）。

**结论**：层数不是瓶颈，**内层走线与过孔密度**才是。改层数前先把内层清空并确认布线器
能把走线全部放到表层，否则只是把一个 157 变成 208。

## 15. 桥接层修复（2026-11）—— 三处会"静默做错事"的缺陷

这三条不是 API 用法问题，而是 MCP/桥接层自身的行为缺陷。它们不会报"执行失败"，
而是悄悄做出错误结果，所以务必理解：

**（1）已送达 EDA 的请求不再自动重发。** 以前桥接一断，MCP 会自动重试同一段代码
最多 5 次。但那时 EasyEDA 里**可能已经执行过一次**——`createComponent()`
会因此生成位号与坐标完全相同的重复器件，图上根本看不出来。
现在返回里带 `delivered: true` / `retrySafe: false`，表示"代码可能已经执行过，
不要直接重提"。先只读查询确认，再决定是否补做。只有 `delivered: false`
（请求从未送达：没窗口、窗口断开、发送失败）才允许安全重试。

**（2）同名窗口换新连接后，旧连接不会再把新连接摘掉。** 新版扩展把 windowId 存在
`sessionStorage` 里，同一标签页重连后 ID 不变。旧的"按 windowId 无脑 pop"写法会在
旧连接关闭时删掉刚注册的新连接，症状是 `/health` 显示 `edaConnected: false`
但 EasyEDA 自己以为已连上，只能等超时。现已按 socket 归属清理。

**（3）显式指定的 `window_id` 不在线时直接报错，不再"粘性接管"改投别的窗口。**
多窗口下静默改投会把代码跑到另一个工程的图纸上——"看起来成功"比明确失败危险得多。
报错里会带 `availableWindows`，用 `eda_list_windows` 看清后重新指定即可。

**（4）文档检索不再漏目录。** 此前索引有 400 文件上限，`format/`、`guide/`、
`user-guide/` 和一部分 `references/types/` 根本没进索引，搜 `TMSchComponent`、
`PAD_NET`、`TPcbPoly` 会 **0 命中**，很容易被误判成"这个 API 不存在"。
现在 `eda_api_search` 覆盖全部 469 份文档，返回里还带 `indexedFiles`。
查格式字段名时如果还是没命中，再按路径用 `eda_api_doc` 直接打开
`format/` 或 `guide/cli.md`。

**（5）MCP SDK 1.x 与 2.x 都能启动。** 1.x 是 `FastMCP`，2.x 是 `MCPServer`，
由 `easyeda_mcp/mcp_compat.py` 适配；`D:/env/mcp`（3.11 + mcp 1.27.1）与
`D:/env/eda`（3.13 + mcp 2.3.0）都验证通过。

**（6）网关执行作用域里没有枚举符号——这一点务必记住。** 扩展是用
`new AsyncFunction('eda', code)` 执行的，函数体只在**全局作用域**求值，看不到扩展包
模块作用域里的枚举常量。实测（专业版 4.1.78）：

```javascript
typeof EPCB_LayerId            // "undefined" —— 直接引用会抛 ReferenceError
Object.keys(eda).length        // 98，没有任何 "E*" 键
Object.keys(globalThis) 里的枚举名  // 0 个
```

也就是说，本文档其它地方"必须用枚举成员、不要写裸数字"的写法，**直接发给网关会 100% 抛错**。
两种正确做法：

1. 照常写枚举（`EPCB_LayerId.TOP`）：`eda_execute` 会自动把用到的枚举以前导
   `const EPCB_LayerId = Object.freeze({...});` 注入到代码前面，并在返回里用
   `injectedEnums` 告诉你注入了哪些；不想注入就传 `inject_enums=false`。
2. 写裸数字：先用 `eda_enum(name="EPCB_LayerId")` 查准确取值（数据来自
   `references/enums/*.md`，78 个枚举 538 个成员），再照数值写。

**回归验证**：`scripts/verify_all_branches.py` —— 按扩展真实报文契约逐分支断言
（309 项），一条不过即退出非 0。改动桥接层后请务必先跑它。

