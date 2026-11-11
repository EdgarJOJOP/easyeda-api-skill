# 嘉立创EDA 专业版 API 技能（Python MCP 版）

面向 AI 智能体的嘉立创EDA（EasyEDA）专业版 API 技能包。本项目是
[easyeda-api-skill](https://github.com/easyeda/easyeda-api-skill) 的 Python 重写：
桥接服务用 **Python 3.10+** 与官方 **mcp** SDK（1.x / 2.x 均可）实现，AI 侧从
"手写 curl 调 HTTP"改为"调用 MCP 工具"，EasyEDA 扩展端保持完全兼容、无需任何改动。

除了 API 查询与代码执行，本包还带了四个面向**真实 PCB 工程**的能力：
DRC 实时守护、通道/反事实分析、事务化移动重布、电路物理仿真（纯 Python，无 numpy/scipy）。

- 📚 **结构化 API 文档** —— 131 个类、78 个枚举、176 个接口、35 个类型别名
- 🧩 **文档源格式规范** —— 工程 / 原理图 / PCB 源文件格式，可直接分析或修改源文件
- 🔌 **MCP 工具链** —— 15 个 `eda_*` 工具 + 3 个文档资源，通过 stdio 或 Streamable HTTP 提供
- 🪟 **WebSocket 桥接** —— 与上游 Node.js 版协议逐字节兼容，端口范围 `49620-49629`
- 🩺 **PCB 工程能力** —— DRC 实时缓存、通道热力图/挡路器件定位、事务化移动重布、电路仿真
- 🤖 **SKILL.md** —— 完整的中文技能说明文件

## 目录结构

```text
D:/eda/
  SKILL.md                  # 智能体技能定义（中文）
  README.md                 # 本文件
  requirements.txt          # 运行依赖
  pyproject.toml            # 打包与入口点定义
  easyeda_mcp/              # Python 包（MCP 服务 + 桥接服务 + 工程分析）
    __init__.py
    __main__.py             # python -m easyeda_mcp 入口
    cli.py                  # 命令行参数解析与启动流程
    protocol.py             # 通信协议常量与报文构造函数
    errors.py               # 异常类型
    bridge.py               # aiohttp 桥接服务（HTTP + WebSocket）
    client.py               # 访问桥接服务的 HTTP 客户端与端口发现
    docs.py                 # 文档检索与读取
    tools.py                # MCP 工具定义（15 个）
    server.py               # 组装 MCPServer（工具 + 资源 + 说明）
    drc_guard.py            # DRC 实时守护：图元事件监听 + 结果缓存 + 增量 diff
    layout_advisor.py       # 通道分析：并查集电气连通 + 栅格 flood fill + 反事实 blockers
    circuit_sim.py          # 电路仿真（解析式）：电源树/纹波/PDN/IR drop/降额
    mna.py                  # 迷你 SPICE：MNA DC/瞬态/AC（纯 Python）
    netlist_builder.py      # 从 EDA 焊盘/网络自动建 SPICE 网表
  scripts/
    run_mcp.py              # 不依赖当前工作目录的启动脚本（MCP 客户端可直接用）
    selftest.py             # 端到端自测脚本
    pcb_finish.py           # PCB 收尾流程（扇出/平面/布线/DRC）
    verify_all_branches.py  # 逐分支穷举验证（MCP 协议 + 工具）
  references/               # API 参考文档（类 / 枚举 / 接口 / 类型）
  format/                   # 工程、原理图、PCB 源文件格式说明
  guide/                    # API 开发指南与扩展开发文档
  user-guide/               # 用户指南
```

## 快速开始

### 1. 安装依赖

```bash
D:/env/eda/python.exe -m pip install -r D:/eda/requirements.txt
D:/env/eda/python.exe -m pip install --no-build-isolation --no-deps -e D:/eda
```

> 也可以直接用现成的解释器，不安装任何东西：
> `D:/env/mcp/python.exe`（Python 3.11 + mcp 1.27.1）与
> `D:/env/eda/python.exe`（Python 3.13 + mcp 2.3.0）都验证过。
> 两个 SDK 大版本的差异由 `easyeda_mcp/mcp_compat.py` 处理。

### 2. 把服务注册到 MCP 客户端

> ⚠️ **`command` 必须是可执行文件路径**，不能写 `cd D:/eda && python -m easyeda_mcp`
> 这类 shell 组合。MCP 客户端不经过 shell 解释，会把整串内容当成文件名。

**方式 A（推荐）：用安装生成的 exe**

```toml
[[plugins]]
name    = "easyeda_mcp"
type    = "stdio"
command = "D:/env/eda/Scripts/easyeda-mcp.exe"
args    = []
```

**方式 B：不安装本地包，用不依赖工作目录的启动脚本**

```toml
[[plugins]]
name    = "easyeda_mcp"
type    = "stdio"
command = "D:/env/eda/python.exe"
args    = ["D:/eda/scripts/run_mcp.py"]
```

**方式 C：`python -m easyeda_mcp` + 显式工作目录**

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

命令行参数：

| 参数 | 说明 |
|------|------|
| `--transport {stdio,streamable-http,none}` | MCP 传输方式，默认 `stdio`；`none` 表示只运行桥接服务 |
| `--bridge {auto,on,off}` | `auto`（默认）复用已有桥接或新建；`on` 强制新建；`off` 只连已有 |
| `--host HOST` | 桥接服务绑定地址，默认 `127.0.0.1` |
| `--port PORT` | 直接绑定指定端口，跳过 `49620-49629` 扫描 |
| `--http-host/--http-port` | Streamable HTTP 传输的监听地址与端口（默认 `8848`） |
| `--log-level {DEBUG,INFO,WARNING,ERROR}` | stderr 日志级别，默认 `INFO` |

### 3. 在 EasyEDA 中连接

安装 `run-api-gateway.eext` 扩展后会自动连接桥接服务：

- https://jlc-ext.com/item/oshwhub/run-api-gateway

### 4. 验证

调用 `eda_status` 看桥接与窗口状态；调用 `eda_ping` 测往返延迟。

## MCP 工具（15 个）

### 连接与执行

| 工具 | 作用 | 关键参数 |
|------|------|----------|
| `eda_status` | 桥接服务与 EasyEDA 连接状态 | — |
| `eda_list_windows` | 列出已连接的 EasyEDA 窗口 | — |
| `eda_select_window` | 设置默认目标窗口 | `window_id` |
| `eda_execute` | 在 EasyEDA 中执行 JavaScript | `code`、`window_id`、`timeout_seconds` |
| `eda_execute_batch` | 一次调用按顺序执行多段代码 | `steps`、`stop_on_error` |
| `eda_ping` | 探测窗口是否在线并测量往返延迟 | `window_id` |

### PCB 工程

| 工具 | 作用 | 关键参数 |
|------|------|----------|
| `eda_pcb_autoroute` | 分批自动布线（断点续跑） | `nets`、`chunk_size`、`layers`、`optimization` |
| `eda_pcb_drc` | **DRC 实时查看**：图元事件缓存 + 增量 diff | `action`（status/check/watch/diff/unwatch/capabilities）、`mode` |
| `eda_pcb_channel_map` | **通道热力图 + 网络可达性** | `gx_count`、`gy_count` |
| `eda_pcb_blockers` | **反事实找挡路器件** | `net`、`radius` |
| `eda_pcb_move_reroute` | **事务化移动重布**（失败回滚） | `des`、`x`、`y`、`rotation` |
| `eda_pcb_circuit_sim` | **电路仿真**：电源树/纹波/PDN/IR drop/降额 + MNA | — |

### 文档

| 工具 | 作用 | 关键参数 |
|------|------|----------|
| `eda_api_search` | 检索 API 文档 | `query`、`limit` |
| `eda_api_doc` | 读取某个文档文件 | `path`、`offset`、`limit` |
| `eda_enum` | 查询枚举成员与取值 | `name` |

MCP 资源：`easyeda://skill`、`easyeda://references/index`、
`easyeda://references/quick-reference`。

## PCB 工程能力详解

### DRC 实时守护（`eda_pcb_drc`）

EasyEDA 4.1.78 没有 v4.2 才引入的 `startRealTimeDrc()`（恒返回 false），而
`pcb_Drc.check()` 单次要 3~8 秒。本工具在 EDA 里注册图元事件监听器，把结果缓存在
扩展的 `globalThis` 中；改完电路再调用它，只在"有变更/缓存过期"时才真正 check：

- `action=status`：默认，脏了或过期就刷新；
- `action=watch`：注册监听，`auto=true` 时变更后 1.5 秒自动重跑；
- `action=diff`：返回相对上次快照新增/修复的违规；
- `mode=auto|realtime|poll`：EDA ≥ 4.2 走原生实时 DRC，低版本降级为"事件 + 主动检查"。

### 通道分析（`eda_pcb_channel_map` / `eda_pcb_blockers`）

两套判定严格分离：

- **电气连通**用精确几何并查集（点-矩形/点-圆/线段-线段距离 ≤ 0.5 mil），判断网络是否已连、删线后是否断开；
- **空间通道**用栅格 flood fill（4/8 mil，位掩码），判断两点之间有没有物理通道；
- `blockers` 做反事实：临时移除候选器件，返回"移开它就能腾出通道"的器件。

### 事务化移动重布（`eda_pcb_move_reroute`）

顺序：快照（位置 + 与焊盘接触的走线）→ 只删这些接触线 → 移动 → `autoRouting` →
EDA DRC 对比 → 变差则位置和线一起回滚。只删接触线（而不是整个网络）是关键：
C1 挂在 GND 上时，按网络删会删掉全板 340+ 段电源地走线。

### 电路仿真（`eda_pcb_circuit_sim`）

纯 Python，不依赖 numpy/scipy，返回两部分：

1. **解析式检查**：电源树设定点（buck 分压/LDO）、buck 纹波、PDN 阻抗（C+ESR+ESL 并联）、
   IR drop、每个元件的降额率（电容电压 ≤80%、电阻功率 ≤50%、电感电流 ≤70%）；
2. **MNA 全板仿真**：从焊盘/网络自动建网表，DC 工作点 + 逐轨 AC 阻抗（1k~100MHz）+
   逐轨上电瞬态（t90 建立时间）。

`mna.py` 支持 R/L/C、独立 V/I 源、受控源、二极管（牛顿迭代），分析类型覆盖 DC/瞬态/AC。

## 兼容的 HTTP 接口

桥接服务的 HTTP 接口与上游 Node.js 版完全一致，方便用 `curl` 或自研脚本调试：

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/health` | 服务身份与连接状态 |
| `GET` | `/eda-windows` | 已连接的 EDA 窗口 |
| `POST` | `/eda-windows/select` | 选择活动窗口 |
| `POST` | `/execute` | 执行代码 |

```bash
curl http://127.0.0.1:49620/health
curl -X POST http://127.0.0.1:49620/execute \
  -H "Content-Type: application/json" \
  -d '{"code": "return await eda.dmt_Project.getCurrentProjectInfo();"}'
```

## 架构

```
┌───────────┐    MCP(stdio)   ┌──────────────────┐   WebSocket    ┌───────────┐
│  AI 智能体 │ ◄────────────► │  easyeda_mcp     │ ◄────────────► │ EasyEDA   │
│ (MCP 客户端)│                │ (Python MCP 服务) │  49620-49629   │ (客户端)  │
└───────────┘                └──────────────────┘                └───────────┘
```

通信协议（与上游一致）：

| 报文类型 | 方向 | 说明 |
|----------|------|------|
| `execute` | AI → EDA | 执行 JavaScript |
| `result` | EDA → AI | 返回执行结果 |
| `error` | EDA → AI | 返回执行错误 |
| `handshake` | 服务端 → 客户端 | 连接校验（含 `service: "easyeda-bridge"`） |
| `register` | EDA → 服务端 | 注册窗口 ID |
| `ping/pong` | 双向 | 心跳 |

## 自测

```bash
cd D:/eda
D:/env/mcp/python.exe scripts/selftest.py             # 冒烟：各工具跑一遍
D:/env/mcp/python.exe scripts/verify_all_branches.py  # 逐分支穷举（MCP 协议 + 工具）
D:/env/mcp/python.exe scripts/verify_live_eda.py      # 真实 EasyEDA 只读联调
```

工程能力的验证脚本在开发工作区（`D:/eda/deepseek/eda`）里，按能力分：

| 脚本 | 覆盖 |
|------|------|
| `verify_drc_branches.py` | DRC 守护 84 项（含 v4.2 分支与低版本降级） |
| `verify_layout_advisor.py` | 通道分析 20 项（合成 15 + 真板 5） |
| `practice_single.py` | 删线→模型与 EDA DRC 一致→恢复（7 项） |
| `practice_blockers.py` | 副本上 J1 挡通道 → blockers 定位（9 项） |
| `practice_move.py` / `verify_circuit_sim.py` | 事务化移动、电路仿真 12 项 |

## 与上游 Node.js 版的差异

| 方面 | 上游（Node.js） | 本项目（Python） |
|------|-----------------|------------------|
| 运行时 | Node.js 18+ / `ws` | Python 3.10+ / `mcp`(1.x 或 2.x) + `aiohttp` |
| AI 侧接口 | HTTP + curl | MCP 工具（stdio / Streamable HTTP），HTTP 接口保留 |
| 服务启动 | `npm run server` | `python -m easyeda_mcp` |
| 文档检索 | 智能体自己 grep | `eda_api_search` / `eda_api_doc` 工具 |
| 多实例 | 检测到已有实例后退出 | 检测到已有实例后复用（MCP 进程不能退出） |
| 工程能力 | 无 | DRC 守护 / 通道分析 / 事务化移动 / 电路仿真 |
| 文档 | 英文 | 中文 |

EasyEDA 扩展（`run-api-gateway.eext`）的连接方式、握手报文与端口范围均未改变，
因此扩展端无需更新。

## 文档

- [SKILL.md](SKILL.md) —— 智能体技能说明（工作流程、代码范式、常见错误）
- [references/_index.md](references/_index.md) —— API 总索引
- [references/_quick-reference.md](references/_quick-reference.md) —— 全部方法签名速查
- [format/index.md](format/index.md) —— 工程源文件格式总览
- [guide/index.md](guide/index.md) —— API 开发指南
