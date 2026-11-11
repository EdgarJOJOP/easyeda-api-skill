"""逐分支穷举的端到端验证（EasyEDA MCP ↔ Run API Gateway 契约）。

为什么需要这个脚本
------------------
``easyeda_mcp`` 的价值只有一个：把 Run API Gateway 扩展暴露的
"在 EDA 里执行一段 JS 并取回 JSON" 原样翻译给 MCP 客户端。
"翻译"一旦有损，表现往往不是报错，而是**静默做错事**：

* 代码被改写一个字符，`return` 的东西就变了；
* 断线后自动重发，图纸上就多出一个同位号、同坐标的重复器件；
* 同一个 windowId 换到新连接时旧连接把新连接摘掉，桥接从此"看不见窗口"；
* 检索索引漏掉 format/guide，智能体就断言"这个 API 不存在"。

因此这里把契约拆成可枚举的分支，逐个断言，一条不过就算失败：

  A 静态契约        —— 常量、报文结构、文档索引完整性与可读性
  B HTTP 端点       —— /health /eda-windows /eda-windows/select /execute 全部分支
  C WebSocket 协议  —— handshake / register / ping / pong / result / error / 异常帧
  D 窗口生命周期    —— 多窗口路由、同名窗口重连接管、断开、显式窗口不存在
  E 代码与结果无损  —— 字节级代码回传、JSON 边界值、5MB 大结果、并发
  F MCP 工具层      —— 8 个工具 + 3 个资源、超时字段、重试安全、批量
  G 文档检索        —— 全目录覆盖、各类查询分支、翻页与路径穿越
  H CLI             —— --version / --bridge off / --transport none / 端口冲突

用法::

    D:/env/mcp/python.exe scripts/verify_all_branches.py
    D:/env/mcp/python.exe scripts/verify_all_branches.py --report report.json -v
"""

from __future__ import annotations

import argparse
import asyncio
import json
import contextlib
import os
import queue
import socket
import subprocess
import sys
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlparse

import aiohttp

#: 支持用 EASYEDA_MCP_ROOT 指向另一份代码（例如修复前的原始副本），
#: 便于对同一套断言做"修复前 / 修复后"对照。
_env_root = os.environ.get("EASYEDA_MCP_ROOT")
ROOT = (
    Path(_env_root).resolve()
    if _env_root
    else Path(__file__).resolve().parent.parent
)
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from easyeda_mcp import protocol as P  # noqa: E402
from easyeda_mcp.bridge import BridgeServer  # noqa: E402
from easyeda_mcp.client import BridgeClient  # noqa: E402
from easyeda_mcp.docs import DOC_ROOTS, DocError, DocIndex  # noqa: E402
from easyeda_mcp.enums import EnumIndex  # noqa: E402
from easyeda_mcp.tools import _code_warnings, _effective_timeout  # noqa: E402

#: 验证专用回环地址。真机扩展只会连 127.0.0.1:49620-49629，
#: 把整套验证放到 127.0.0.2 上就不会被"真机抢连"，断言才稳定可重复。
HOST = "127.0.0.2"

#: 契约常量（摘自 easyeda/eext-run-api-gateway 的 src/index.ts） ──────
EXT_SERVICE_ID = "easyeda-bridge"
EXT_PORT_START = 49620
EXT_PORT_END = 49629
EXT_WS_PATH = "/eda"
EXT_MSG_TYPES = {
    "execute",
    "result",
    "error",
    "ping",
    "pong",
    "handshake",
    "register",
}

# ─── FakeEda 的行为标记（等价于 EDA 里那段脚本的结果） ──────────────────
M_ERROR = "__EDA_ERROR__"  # 扩展抛错 -> error 报文
M_UNDEF = "__EDA_UNDEF__"  # 返回 undefined -> 扩展归一成 null
M_DROP = "__EDA_DROP__"  # 执行期间连接断开，不发结果
M_HUGE = "__EDA_HUGE__:"  # 返回巨型字符串
M_RESULT = "__EDA_RESULT__:"  # 返回指定 JSON
M_SLOW = "__EDA_SLOW__:"  # 延迟指定秒数再回复


def free_port() -> int:
    """向系统申请一个空闲端口。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((HOST, port))
        except OSError:
            return False
        return True


async def pick_bridge_port() -> int:
    """在扩展的端口范围内挑一个空闲端口（绑定在验证专用地址 HOST 上）。

    必须落在 49620-49629：MCP 子进程靠这段范围发现桥接。若该地址上已有别的
    桥接实例在跑，直接拒绝——否则验证会打到别人的实例上，结论没有意义。
    """
    timeout = aiohttp.ClientTimeout(total=0.4)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        for port in range(EXT_PORT_START, EXT_PORT_END + 1):
            try:
                async with session.get(f"http://{HOST}:{port}/health") as resp:
                    body = await resp.json(content_type=None)
                if isinstance(body, dict) and body.get("service") == EXT_SERVICE_ID:
                    raise SystemExit(
                        f"{HOST}:{port} 上已有 EasyEDA 桥接服务在运行。"
                        "请先停止它（或关闭正在运行的 MCP 服务）再执行本验证，"
                        "否则断言会落到另一个实例上。"
                    )
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
                pass
            if _port_free(port):
                return port
    raise SystemExit(
        f"{HOST} 的 {EXT_PORT_START}-{EXT_PORT_END} 全部被占用，无法启动验证用桥接服务。"
    )


class Suite:
    """极简断言收集器：单项失败不中断，最后统一裁决。"""

    def __init__(self, verbose: bool = False) -> None:
        self.section = ""
        self.verbose = verbose
        self.records: list[dict[str, Any]] = []
        self.passed = 0
        self.failed = 0

    def begin(self, section: str) -> None:
        self.section = section
        print(f"\n=== {section} ===")

    def check(self, name: str, ok: Any, detail: Any = "") -> bool:
        ok = bool(ok)
        record = {
            "section": self.section,
            "name": name,
            "ok": ok,
            "detail": "" if ok else _short(detail),
        }
        self.records.append(record)
        if ok:
            self.passed += 1
            if self.verbose:
                print(f"  [PASS] {name}")
        else:
            self.failed += 1
            print(f"  [FAIL] {name}" + (f"  -> {record['detail']}" if detail != "" else ""))
        return ok

    def equal(self, name: str, actual: Any, expected: Any) -> bool:
        return self.check(
            name, actual == expected, f"actual={_short(actual)} expected={_short(expected)}"
        )

    def fail(self, name: str, detail: Any) -> None:
        self.check(name, False, detail)

    @property
    def total(self) -> int:
        return self.passed + self.failed


def _short(value: Any, limit: int = 300) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, default=repr)
    except (TypeError, ValueError):
        text = repr(value)
    return text if len(text) <= limit else text[:limit] + f"…(+{len(text) - limit})"


# ─── MCP 传输：见 scripts/mcp_stdio_compat.py ─────────────────────────
#
# 官方 stdio 客户端依赖 anyio 的 overlapped 命名管道，受限环境会 WinError 5；
# 公共模块里有等价实现（普通匿名管道 + 子进程），并会在有权限时自动切回官方实现。

from mcp_stdio_compat import (  # noqa: E402
    detect as _probe_sdk_stdio,
    open_session as _open_session_compat,
    open_stdio as _open_stdio_compat,
)

#: 仅供本脚本打印"当前用的是哪种传输"。
_SDK_STDIO_OK = False

# ─── FakeEda：忠实复刻扩展 src/index.ts 的行为 ─────────────────────────



class FakeEda:
    """模拟 Run API Gateway 扩展。

    对齐点（来自扩展源码）：

    * 连 ``ws://127.0.0.1:<port>/eda``，等服务端 handshake；
    * 校验 ``service === 'easyeda-bridge'`` 后发 ``register``（windowId 在同
      一"标签页"内不变，模拟 sessionStorage）；
    * 每 ``ping_interval`` 秒发一次 ``ping``，期待 ``pong``；
    * 收到 ``execute`` 时按 ``new AsyncFunction('eda', code)`` 语义执行，
      结果 **先经 JSON 序列化** 再发 ``result``；``undefined`` 归一成 ``null``；
    * 执行抛错发 ``error``（只有 message）。
    """

    def __init__(
        self,
        port: int,
        window_id: str,
        *,
        ping_interval: float = 0.0,
        name: str = "eda",
    ) -> None:
        self.port = port
        self.window_id = window_id
        self.ping_interval = ping_interval
        self.name = name
        self.handshake: dict[str, Any] | None = None
        self.codes: list[str] = []
        self.results_sent: list[Any] = []
        self.pings = 0
        self.pongs = 0
        self.registered = asyncio.Event()
        self.connected = asyncio.Event()
        self._session: aiohttp.ClientSession | None = None
        self.ws: aiohttp.ClientWebSocketResponse | None = None
        self._task: asyncio.Task[None] | None = None
        self._ping_task: asyncio.Task[None] | None = None
        self.error: str | None = None

    async def start(self) -> "FakeEda":
        self._session = aiohttp.ClientSession()
        await self._connect()
        self._task = asyncio.create_task(self._loop())
        if self.ping_interval > 0:
            self._ping_task = asyncio.create_task(self._ping_loop())
        return self

    async def _connect(self) -> None:
        assert self._session is not None
        self.ws = await self._session.ws_connect(
            f"ws://{HOST}:{self.port}{EXT_WS_PATH}", max_msg_size=0
        )
        self.connected.set()
        self.handshake = await self.ws.receive_json()
        await self.ws.send_json(
            {"type": "register", "windowId": self.window_id, "timestamp": P.now_ms()}
        )
        self.registered.set()

    async def _ping_loop(self) -> None:
        """扩展的心跳：每 ping_interval 秒发一次 ping，靠 pong 续命。"""
        try:
            while True:
                await asyncio.sleep(self.ping_interval)
                self.pings += 1
                await self._send(
                    {"type": "ping", "id": f"hb-{self.pings}", "timestamp": P.now_ms()}
                )
        except asyncio.CancelledError:
            pass

    async def _loop(self) -> None:
        assert self.ws is not None
        try:
            async for message in self.ws:
                if message.type is not aiohttp.WSMsgType.TEXT:
                    continue
                payload = json.loads(message.data)
                await self._handle(payload)
        except (aiohttp.ClientError, asyncio.CancelledError, ConnectionResetError):
            pass
        except Exception as exc:  # noqa: BLE001 - 记录后继续，避免静默退出
            self.error = f"{type(exc).__name__}: {exc}"

    async def _handle(self, payload: dict[str, Any]) -> None:
        kind = payload.get("type")
        if kind == "ping":
            self.pings += 1
            await self._send({"type": "pong", "id": payload.get("id"), "timestamp": P.now_ms()})
            return
        if kind == "pong":
            self.pongs += 1
            return
        if kind != "execute":
            return
        code = payload.get("code", "")
        self.codes.append(code)
        request_id = payload.get("id")

        if M_DROP in code:
            await self._close_socket()
            return
        if code.startswith(M_SLOW):
            await asyncio.sleep(float(code.split(":", 1)[1]))
            # 故意仍发回结果：用来确认"超时后迟到的结果被安全忽略"
        if M_ERROR in code:
            await self._send(
                {"type": "error", "id": request_id, "error": "模拟 EDA 内部报错"}
            )
            return
        if M_UNDEF in code:
            await self._send({"type": "result", "id": request_id, "result": None})
            return
        if code.startswith(M_HUGE):
            size = int(code.split(":", 1)[1])
            await self._send(
                {"type": "result", "id": request_id, "result": "x" * size}
            )
            return
        if code.startswith(M_RESULT):
            data = json.loads(code.split(":", 1)[1])
            await self._send({"type": "result", "id": request_id, "result": data})
            return
        await self._send(
            {
                "type": "result",
                "id": request_id,
                "result": {"echo": code, "windowId": self.window_id, "len": len(code)},
            }
        )

    async def _send(self, payload: dict[str, Any]) -> None:
        try:
            text = json.dumps(payload)
        except (TypeError, ValueError):
            return
        if self.ws is None or self.ws.closed:
            return
        try:
            await self.ws.send_str(text)
            if payload.get("type") == "result":
                self.results_sent.append(payload.get("result"))
        except (aiohttp.ClientError, ConnectionResetError):
            pass

    async def _close_socket(self) -> None:
        if self.ws is not None and not self.ws.closed:
            await self.ws.close()

    async def stop(self) -> None:
        if self._ping_task is not None:
            self._ping_task.cancel()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        await self._close_socket()
        if self._session is not None:
            await self._session.close()


async def wait_connected(client: BridgeClient, *, expect: int, timeout: float = 10.0) -> bool:
    """等待桥接服务报告的窗口数达到期望值。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            payload = await client.list_windows()
            if payload.get("count") == expect:
                return True
        except Exception:  # noqa: BLE001 - 桥接可能还在启动
            pass
        await asyncio.sleep(0.1)
    return False


# ─── A. 静态契约 ───────────────────────────────────────────────────────


def section_a(suite: Suite, root: Path) -> DocIndex:
    suite.begin("A 静态契约（不联网）")
    suite.equal("服务标识与扩展一致", P.SERVICE_ID, EXT_SERVICE_ID)
    suite.equal("端口下界与扩展一致", P.PORT_START, EXT_PORT_START)
    suite.equal("端口上界与扩展一致", P.PORT_END, EXT_PORT_END)
    suite.check(
        "报文类型覆盖扩展全部 7 种",
        EXT_MSG_TYPES
        <= {
            P.MSG_EXECUTE,
            P.MSG_RESULT,
            P.MSG_ERROR,
            P.MSG_PING,
            P.MSG_PONG,
            P.MSG_HANDSHAKE,
            P.MSG_REGISTER,
        },
    )

    handshake = P.handshake_message("eda")
    suite.equal("handshake.type", handshake.get("type"), "handshake")
    suite.equal("handshake.service", handshake.get("service"), EXT_SERVICE_ID)
    suite.equal("handshake.clientType", handshake.get("clientType"), "eda")
    suite.check("handshake 带时间戳", isinstance(handshake.get("timestamp"), int))

    execute = P.execute_message("rid-1", "return 1;", "win-1")
    suite.equal("execute.type", execute.get("type"), "execute")
    suite.equal("execute.id", execute.get("id"), "rid-1")
    suite.equal("execute.code", execute.get("code"), "return 1;")
    suite.equal("execute.windowId", execute.get("windowId"), "win-1")

    pong = P.pong_message("hb-1")
    suite.equal("pong.type", pong.get("type"), "pong")
    suite.equal("pong 回显 ping 的 id", pong.get("id"), "hb-1")

    suite.check(
        "超时上限 >= 官方 CLI 的 30 分钟以内且不为 0",
        P.MAX_REQUEST_TIMEOUT >= 600 and P.MAX_REQUEST_TIMEOUT <= 1800,
        P.MAX_REQUEST_TIMEOUT,
    )
    suite.check("默认超时 30s", P.DEFAULT_REQUEST_TIMEOUT == 30.0)

    # 文档索引完整性
    index = DocIndex(root)
    files = index.files()
    expected: list[str] = []
    for name in DOC_ROOTS:
        base = root / name
        if base.is_dir():
            expected.extend(
                path.relative_to(root).as_posix()
                for path in sorted(base.rglob("*.md"))
                if path.is_file()
            )
    suite.equal("文档索引覆盖全部 markdown", len(files), len(expected))
    for name in DOC_ROOTS:
        suite.check(
            f"文档根目录已索引：{name}",
            any(f.startswith(name + "/") for f in files),
            [f for f in files if f.startswith(name + "/")][:2],
        )
    suite.check(
        "索引包含 format/index.md", "format/index.md" in files, files[:3]
    )
    suite.check(
        "索引包含 references/types 下的全部类型文档",
        sum(1 for f in files if f.startswith("references/types/"))
        == len(list((root / "references" / "types").glob("*.md"))),
    )

    # 每份文档都能通过 read() 读到（不会因编码/路径问题失败）
    unreadable: list[str] = []
    empty: list[str] = []
    for relative in files:
        try:
            payload = index.read(relative, offset=0, limit=1)
        except DocError as exc:
            unreadable.append(f"{relative}: {exc}")
            continue
        if payload.get("totalLines", 0) <= 0:
            empty.append(relative)
    suite.check("全部文档可读", not unreadable, unreadable[:3])
    suite.check("没有空文档", not empty, empty[:3])
    return index


# ─── B. HTTP 端点 ──────────────────────────────────────────────────────


async def section_b(suite: Suite, port: int) -> None:
    suite.begin("B HTTP 端点（桥接无 EDA 连接）")
    base = f"http://{HOST}:{port}"

    async with aiohttp.ClientSession() as session:
        # B1 /health
        async with session.get(base + "/health") as resp:
            body = await resp.json()
            suite.equal("B1 /health 200", resp.status, 200)
            suite.equal("B1 service", body.get("service"), EXT_SERVICE_ID)
            suite.equal("B1 status", body.get("status"), "ok")
            suite.equal("B1 无窗口时 edaConnected=false", body.get("edaConnected"), False)
            suite.equal("B1 edaWindowCount", body.get("edaWindowCount"), 0)
            suite.equal("B1 pendingRequests", body.get("pendingRequests"), 0)
            suite.check("B1 带 CORS 头", resp.headers.get("Access-Control-Allow-Origin") == "*")
            upstream_keys = {
                "service",
                "status",
                "edaConnected",
                "edaWindowCount",
                "activeWindowId",
                "pendingRequests",
                "timestamp",
            }
            suite.check(
                "B1 保留上游全部字段（可增不可减）",
                upstream_keys <= set(body),
                sorted(upstream_keys - set(body)),
            )

        # B2 /eda-windows
        async with session.get(base + "/eda-windows") as resp:
            body = await resp.json()
            suite.equal("B2 /eda-windows 200", resp.status, 200)
            suite.equal("B2 count", body.get("count"), 0)
            suite.equal("B2 windows", body.get("windows"), [])
            suite.check(
                "B2 字段集合与上游一致",
                set(body) >= {"windows", "activeWindowId", "count"},
                sorted(body),
            )

        # B3 选择不存在的窗口
        async with session.post(base + "/eda-windows/select", json={"windowId": "nope"}) as resp:
            body = await resp.json()
            suite.equal("B3 select 未知窗口 -> 404", resp.status, 404)
            suite.check("B3 返回 error 字段", isinstance(body.get("error"), str), body)

        # B4 非法 JSON
        async with session.post(base + "/eda-windows/select", data=b"{oops") as resp:
            suite.equal("B4 select 非法 JSON -> 400", resp.status, 400)

        # B5 windowId 非字符串
        async with session.post(base + "/eda-windows/select", json={"windowId": 1}) as resp:
            suite.equal("B5 select 非字符串 windowId -> 404", resp.status, 404)

        # B6 /execute 缺少 code
        async with session.post(base + "/execute", json={}) as resp:
            body = await resp.json()
            suite.equal("B6 execute 缺 code -> 400", resp.status, 400)
            suite.check("B6 错误信息提到 code", '"code"' in str(body.get("error")), body)

        # B7 code 非字符串
        async with session.post(base + "/execute", json={"code": 1}) as resp:
            suite.equal("B7 execute code 非字符串 -> 400", resp.status, 400)

        # B8 非法 JSON
        async with session.post(base + "/execute", data=b"not json") as resp:
            suite.equal("B8 execute 非法 JSON -> 400", resp.status, 400)

        # B9 windowId 非字符串
        async with session.post(base + "/execute", json={"code": "return 1", "windowId": 2}) as resp:
            suite.equal("B9 execute windowId 非字符串 -> 400", resp.status, 400)

        # B10 timeout 非数字
        async with session.post(
            base + "/execute", json={"code": "return 1", "timeout": "abc"}
        ) as resp:
            suite.equal("B10 timeout 非数字 -> 400", resp.status, 400)

        # B11 timeout <= 0
        async with session.post(
            base + "/execute", json={"code": "return 1", "timeout": 0}
        ) as resp:
            suite.equal("B11 timeout<=0 -> 400", resp.status, 400)

        # B12 无 EDA 时 execute -> 503 且 delivered=false
        async with session.post(base + "/execute", json={"code": "return 1"}) as resp:
            body = await resp.json()
            suite.equal("B12 无 EDA -> 503", resp.status, 503)
            suite.equal("B12 success=false", body.get("success"), False)
            suite.equal("B12 delivered=false（可安全重试）", body.get("delivered"), False)
            suite.check(
                "B12 提示安装扩展",
                "run-api-gateway" in str(body.get("error")),
                body.get("error"),
            )

        # B13 未知路径
        async with session.get(base + "/nope") as resp:
            suite.equal("B13 未知路径 -> 404", resp.status, 404)
            suite.check("B13 返回 JSON", resp.content_type == "application/json")

        # B14 OPTIONS 预检
        async with session.options(base + "/execute") as resp:
            suite.equal("B14 OPTIONS -> 204", resp.status, 204)
            suite.equal(
                "B14 允许的方法",
                resp.headers.get("Access-Control-Allow-Methods"),
                "POST, GET, OPTIONS",
            )

        # B15 方法不匹配（GET /execute）
        async with session.get(base + "/execute") as resp:
            suite.equal("B15 GET /execute -> 404", resp.status, 404)

        # B16 非 WebSocket 升级访问 /eda
        async with session.get(base + "/eda") as resp:
            suite.equal("B16 GET /eda（无 Upgrade）-> 404", resp.status, 404)

        # B17 超大请求体（上游 Node 无 1MB 限制）
        big = "return '" + "A" * (2 * 1024 * 1024) + "';"
        async with session.post(base + "/execute", json={"code": big}) as resp:
            suite.check(
                "B17 2MB 代码不会被 413 拒绝",
                resp.status != 413,
                f"status={resp.status}",
            )


# ─── C. WebSocket 协议 ────────────────────────────────────────────────


async def section_c(suite: Suite, port: int) -> None:
    suite.begin("C WebSocket 协议")

    async with aiohttp.ClientSession() as session:
        # C1 EDA 路径握手
        ws = await session.ws_connect(f"ws://{HOST}:{port}/eda", max_msg_size=0)
        handshake = await ws.receive_json()
        suite.equal("C1 handshake.type", handshake.get("type"), "handshake")
        suite.equal("C1 handshake.service", handshake.get("service"), EXT_SERVICE_ID)
        suite.equal("C1 clientType=eda", handshake.get("clientType"), "eda")

        # C2 ping -> pong（扩展心跳依赖它续命）
        await ws.send_str(json.dumps({"type": "ping", "id": "hb-1"}))
        pong = await ws.receive_json()
        suite.equal("C2 pong.type", pong.get("type"), "pong")
        suite.equal("C2 pong 回显 id", pong.get("id"), "hb-1")

        # C3 非 JSON 文本帧被忽略且连接不断
        await ws.send_str("this is not json")
        await ws.send_str(json.dumps({"type": "ping", "id": "hb-2"}))
        pong = await ws.receive_json()
        suite.equal("C3 非 JSON 帧之后连接仍可用", pong.get("id"), "hb-2")

        # C4 二进制 JSON 帧
        await ws.send_bytes(json.dumps({"type": "ping", "id": "hb-3"}).encode())
        pong = await ws.receive_json()
        suite.equal("C4 二进制帧按 JSON 解析", pong.get("id"), "hb-3")

        # C5 未知报文类型被忽略
        await ws.send_str(json.dumps({"type": "what-is-this", "id": "x"}))
        await ws.send_str(json.dumps({"type": "ping", "id": "hb-4"}))
        pong = await ws.receive_json()
        suite.equal("C5 未知类型被忽略", pong.get("id"), "hb-4")

        # C6 register 之前的 ping 也能得到 pong（不会抛异常）
        await ws.close()

        ws2 = await session.ws_connect(f"ws://{HOST}:{port}/eda", max_msg_size=0)
        await ws2.receive_json()
        await ws2.send_str(json.dumps({"type": "ping", "id": "pre-reg"}))
        pong = await ws2.receive_json()
        suite.equal("C6 未注册连接也能收到 pong", pong.get("id"), "pre-reg")
        await ws2.close()

        # C7 agent 路径握手 + execute/result 往返
        agent = await session.ws_connect(f"ws://{HOST}:{port}/agent", max_msg_size=0)
        hs = await agent.receive_json()
        suite.equal("C7 agent clientType", hs.get("clientType"), "agent")
        await agent.send_str(
            json.dumps({"type": "execute", "id": "a-1", "code": "return 1;"})
        )
        reply = await agent.receive_json()
        suite.equal("C7 无 EDA 时返回 error 报文", reply.get("type"), "error")
        suite.equal("C7 error 回显 id", reply.get("id"), "a-1")

        # C8 agent 缺 code
        await agent.send_str(json.dumps({"type": "execute", "id": "a-2"}))
        reply = await agent.receive_json()
        suite.equal("C8 agent 缺 code -> error", reply.get("type"), "error")

        # C9 agent ping
        await agent.send_str(json.dumps({"type": "ping", "id": "a-3"}))
        reply = await agent.receive_json()
        suite.equal("C9 agent ping -> pong", reply.get("type"), "pong")
        await agent.close()


# ─── D. 窗口生命周期 ──────────────────────────────────────────────────


async def section_d(suite: Suite, port: int, client: BridgeClient) -> None:
    suite.begin("D 窗口生命周期")

    w1 = await FakeEda(port, "win-A").start()
    suite.check("D1 窗口注册后可见", await wait_connected(client, expect=1), "1s 内未看到窗口")
    health = await client.health()
    suite.equal("D1 activeWindowId 自动选中", health.get("activeWindowId"), "win-A")
    windows = await client.list_windows()
    suite.equal("D1 窗口列表数量", windows.get("count"), 1)
    suite.equal("D1 窗口条目 windowId", windows["windows"][0].get("windowId"), "win-A")
    suite.equal("D1 窗口条目 connected", windows["windows"][0].get("connected"), True)
    suite.equal("D1 窗口条目 active", windows["windows"][0].get("active"), True)

    # D2 第二个窗口
    w2 = await FakeEda(port, "win-B").start()
    suite.check("D2 第二个窗口注册", await wait_connected(client, expect=2))
    health = await client.health()
    suite.equal("D2 活动窗口保持不变", health.get("activeWindowId"), "win-A")

    # D3 路由：指定窗口只发到该窗口
    await client.execute("return 'to-B';", window_id="win-B")
    suite.check("D3 代码送到了 win-B", "return 'to-B';" in w2.codes, w2.codes)
    suite.check("D3 没有污染 win-A", "return 'to-B';" not in w1.codes, w1.codes)

    # D4 切换活动窗口
    selected = await client.select_window("win-B")
    suite.equal("D4 切换成功", selected.get("activeWindowId"), "win-B")
    await client.execute("return 'active-B';")
    suite.check("D4 不指定窗口时发往活动窗口", "return 'active-B';" in w2.codes)

    # D5 显式指定不存在的窗口：必须报错，绝不静默改投
    before_a, before_b = len(w1.codes), len(w2.codes)
    error_text = ""
    try:
        await client.execute("return 'ghost';", window_id="ghost-window")
        suite.fail("D5 显式窗口不存在时应报错", "没有抛异常")
    except Exception as exc:  # noqa: BLE001
        error_text = str(exc)
        suite.check("D5 显式窗口不存在时报错", True)
    suite.check("D5 错误里给出可用窗口", "win-A" in error_text or "win-B" in error_text, error_text)
    suite.check("D5 没有任何窗口收到这段代码", len(w1.codes) == before_a and len(w2.codes) == before_b)

    # D6 显式窗口已断开同样报错（不静默改投）
    try:
        await client.execute("return 'gone';", window_id="win-A")
        suite.check("D6 存活窗口仍可指定", True)
    except Exception as exc:  # noqa: BLE001
        suite.fail("D6 存活窗口仍可指定", exc)

    # D7 同名窗口换新连接接管：旧连接关闭不能影响新连接
    old = w1
    new = await FakeEda(port, "win-A").start()
    await asyncio.sleep(0.2)
    await old.stop()
    await asyncio.sleep(0.4)
    health = await client.health()
    suite.equal("D7 旧连接断开后同名窗口仍在线", health.get("edaConnected"), True)
    suite.check(
        "D7 窗口数量没有因旧连接断开而减少",
        health.get("edaWindowCount") == 2,
        health.get("edaWindowCount"),
    )
    try:
        outcome = await client.execute("return 'after-supersede';", window_id="win-A")
        suite.check("D7 接管后仍能执行", outcome is not None)
    except Exception as exc:  # noqa: BLE001
        suite.fail("D7 接管后仍能执行", exc)
    suite.check(
        "D7 新连接收到了这段代码", "return 'after-supersede';" in new.codes, new.codes
    )

    # D8 断开时报 delivered=true（代码可能已执行，禁止自动重试）
    task = asyncio.create_task(client.execute(M_DROP + " return 1;"))
    await asyncio.sleep(0.4)
    try:
        await task
        suite.fail("D8 断开时应抛错", "正常返回了")
    except Exception as exc:  # noqa: BLE001
        suite.check("D8 断开时抛 EdaNotConnectedError", type(exc).__name__ == "EdaNotConnectedError", type(exc).__name__)
        suite.equal("D8 delivered=true", getattr(exc, "delivered", None), True)

    await new.stop()
    await w2.stop()
    suite.check("D9 全部断开后窗口数归零", await wait_connected(client, expect=0))
    health = await client.health()
    suite.equal("D9 edaConnected=false", health.get("edaConnected"), False)
    suite.equal("D9 activeWindowId 清空", health.get("activeWindowId"), None)


# ─── E. 代码与结果无损 ────────────────────────────────────────────────


TRICKY_CODES: list[tuple[str, str]] = [
    ("单行", "return 1;"),
    ("多行", "const a = 1;\nconst b = 2;\nreturn a + b;"),
    ("CRLF", "const a = 1;\r\nreturn a;"),
    ("双引号与反斜杠", 'return "C:\\\\Temp\\\\a.js";'),
    ("单引号与模板串", "const s = `x${1 + 1}`;\nreturn s + '\\n';"),
    ("中文与 emoji", "return '中文 ✓ 🎉 覆铜';"),
    ("形似注释的字符串", "return '// not a comment /* nor this */';"),
    ("JSON 片段", 'return JSON.stringify({"a":[1,2,{"b":null}]});'),
    ("制表符与多余空白", "return \t  'x'\t;  "),
    ("长代码 200KB", "return '" + "B" * 200_000 + "';"),
]


async def section_e(suite: Suite, port: int, client: BridgeClient) -> None:
    suite.begin("E 代码与结果无损")
    eda = await FakeEda(port, "win-E").start()
    await wait_connected(client, expect=1)

    # E1 代码字节级回传（桥接任何改写都会在这里暴露）
    for label, code in TRICKY_CODES:
        before = len(eda.codes)
        outcome = await client.execute(code)
        received = eda.codes[before] if len(eda.codes) > before else None
        suite.check(f"E1 代码原样送达：{label}", received == code, f"收到={_short(received)}")
        payload = outcome.result if isinstance(outcome.result, dict) else {}
        suite.equal(f"E1 结果回显一致：{label}", payload.get("echo"), code)
        suite.equal(f"E1 结果长度一致：{label}", payload.get("len"), len(code))

    # E2 结果 JSON 边界
    cases: list[tuple[str, Any]] = [
        ("null", None),
        ("布尔", True),
        ("整数", 42),
        ("浮点", 1.5),
        ("大浮点", 1e20),
        ("负数", -7),
        ("空数组", []),
        ("空对象", {}),
        ("嵌套", {"a": [1, {"b": None}], "c": "中文"}),
        ("数字键字符串", {"1": "x", "2": [0]}),
        ("长字符串", "y" * 10000),
    ]
    for label, value in cases:
        code = M_RESULT + json.dumps(value, ensure_ascii=False)
        outcome = await client.execute(code)
        suite.equal(f"E2 结果保真：{label}", outcome.result, value)

    # E3 5MB 大结果（默认 4MB 帧上限会在这里断连）
    outcome = await client.execute(M_HUGE + str(5 * 1024 * 1024), timeout=30)
    suite.check(
        "E3 5MB 结果完整到达",
        isinstance(outcome.result, str) and len(outcome.result) == 5 * 1024 * 1024,
        len(outcome.result) if isinstance(outcome.result, str) else type(outcome.result),
    )

    # E4 并发：12 个请求的结果不能串
    codes = [f"return {index};" for index in range(12)]
    outcomes = await asyncio.gather(*(client.execute(code) for code in codes))
    suite.check(
        "E4 并发请求结果一一对应",
        all(
            isinstance(outcome.result, dict) and outcome.result.get("echo") == code
            for outcome, code in zip(outcomes, codes)
        ),
        [outcome.result for outcome in outcomes][:2],
    )

    # E5 超时字段完整
    started = time.perf_counter()
    try:
        await client.execute(M_SLOW + "1.5", timeout=0.5)
        suite.fail("E5 超时应抛错", "正常返回")
    except Exception as exc:  # noqa: BLE001
        elapsed = time.perf_counter() - started
        suite.check("E5 抛 EdaTimeoutError", type(exc).__name__ == "EdaTimeoutError", type(exc).__name__)
        suite.check("E5 requestId 非空", bool(getattr(exc, "request_id", None)), getattr(exc, "request_id", None))
        suite.equal("E5 timeout_s 透传", getattr(exc, "timeout_s", None), 0.5)
        suite.check("E5 超时时间接近 0.5s", 0.4 <= elapsed <= 2.0, round(elapsed, 2))

    # E6 超时后迟到的结果被安全忽略，且不影响后续请求
    await asyncio.sleep(1.3)
    outcome = await client.execute("return 'after-timeout';")
    suite.equal("E6 超时后桥接仍可用", outcome.result.get("echo"), "return 'after-timeout';")

    # E7 EDA 侧抛错 -> BridgeError，且 errorType 可辨识
    try:
        await client.execute(M_ERROR)
        suite.fail("E7 EDA 报错应上抛", "正常返回")
    except Exception as exc:  # noqa: BLE001
        suite.equal("E7 错误信息透传", "模拟 EDA 内部报错" in str(exc), True)

    await eda.stop()


# ─── F. MCP 工具层 ────────────────────────────────────────────────────


def tool_payload(result: Any) -> dict[str, Any]:
    """读取 CallToolResult 中的结构化载荷（兼容 mcp 1.x / 2.x）。"""
    data = getattr(result, "structuredContent", None)
    if data is None:
        data = getattr(result, "structured_content", None)
    if isinstance(data, dict):
        if set(data) == {"result"} and isinstance(data["result"], dict):
            return data["result"]
        return data
    for block in getattr(result, "content", []) or []:
        text = getattr(block, "text", None)
        if text:
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return {"text": text}
    return {}


async def call(session: Any, tool: str, **arguments: Any) -> dict[str, Any]:
    """调用一个 MCP 工具并返回结构化载荷。

    第一个参数叫 ``tool`` 而不是 ``name``：有些工具自身有 ``name`` 参数
    （例如 ``eda_enum``），用 ``name`` 会撞名。
    """
    return tool_payload(await session.call_tool(tool, arguments))


async def section_f(suite: Suite, port: int, python: Path, root: Path, index: DocIndex) -> None:
    suite.begin("F MCP 工具层（真实 stdio 子进程）")
    from mcp import StdioServerParameters

    params = StdioServerParameters(
        command=str(python),
        args=["-m", "easyeda_mcp", "--host", HOST, "--log-level", "WARNING"],
        cwd=str(root),
    )
    async with _open_stdio_compat(params) as (read_stream, write_stream):
        async with _open_session_compat(read_stream, write_stream) as session:
            await session.initialize()

            tools = await session.list_tools()
            names = {tool.name for tool in tools.tools}
            expected = {
                "eda_status",
                "eda_list_windows",
                "eda_select_window",
                "eda_execute",
                "eda_execute_batch",
                "eda_ping",
                "eda_api_search",
                "eda_api_doc",
                "eda_enum",
            }
            suite.check("F1 8 个工具全部注册", expected <= names, sorted(names))

            resources = await session.list_resources()
            uris = {str(item.uri) for item in resources.resources}
            suite.check(
                "F1 3 个文档资源全部注册",
                {"easyeda://skill", "easyeda://references/index", "easyeda://references/quick-reference"}
                <= uris,
                sorted(uris),
            )
            prompts = await session.list_prompts()
            suite.check("F1 prompt 列表可查询（空也正常）", prompts is not None)

            # F2 无窗口状态
            status = await call(session, "eda_status")
            suite.equal("F2 bridge.running", status.get("bridge", {}).get("running"), True)
            suite.equal("F2 eda.connected=false", status.get("eda", {}).get("connected"), False)
            suite.check("F2 给出未连接提示", bool(status.get("hint")), status.get("hint"))
            no_eda = await call(session, "eda_execute", code="return 1;")
            suite.equal("F2 无窗口执行失败", no_eda.get("ok"), False)
            suite.equal("F2 无窗口时 delivered=false", no_eda.get("delivered"), False)
            suite.equal("F2 无窗口时 retrySafe=true", no_eda.get("retrySafe"), True)

            windows = await call(session, "eda_list_windows")
            suite.equal("F2 无窗口列表 count=0", windows.get("count"), 0)

            # F3 接入 EDA
            eda = await FakeEda(port, "win-MCP", ping_interval=0.3).start()
            await wait_connected(BridgeClient(host=HOST, port=port), expect=1)
            status = await call(session, "eda_status")
            suite.equal("F3 检测到窗口", status.get("eda", {}).get("windowCount"), 1)
            suite.equal(
                "F3 活动窗口", status.get("eda", {}).get("activeWindowId"), "win-MCP"
            )
            suite.check("F3 上报 MCP SDK 版本", bool(status.get("bridge", {}).get("mcpSdk")), status.get("bridge"))

            # F4 代码与结果经 MCP 全链路保真
            for label, code in TRICKY_CODES:
                before = len(eda.codes)
                outcome = await call(session, "eda_execute", code=code)
                suite.equal(f"F4 执行成功：{label}", outcome.get("ok"), True)
                suite.equal(f"F4 代码未改写：{label}", eda.codes[before] if len(eda.codes) > before else None, code)
                suite.equal(
                    f"F4 结果保真：{label}",
                    (outcome.get("result") or {}).get("echo"),
                    code,
                )
                suite.equal(f"F4 指明窗口：{label}", outcome.get("windowId"), "win-MCP")

            # F5 EDA 报错
            failure = await call(session, "eda_execute", code=M_ERROR)
            suite.equal("F5 ok=false", failure.get("ok"), False)
            suite.equal("F5 errorType=BridgeError", failure.get("errorType"), "BridgeError")
            suite.check(
                "F5 错误信息透传",
                "模拟 EDA 内部报错" in str(failure.get("error")),
                failure.get("error"),
            )

            # F6 undefined -> null
            undef = await call(session, "eda_execute", code=M_UNDEF)
            suite.equal("F6 undefined 归一为 null", undef.get("result"), None)

            # F7 超时字段
            timeout = await call(session, "eda_execute", code=M_SLOW + "2", timeout_seconds=0.6)
            suite.equal("F7 超时 ok=false", timeout.get("ok"), False)
            suite.equal("F7 timeout=true", timeout.get("timeout"), True)
            suite.equal("F7 stillRunning=true", timeout.get("stillRunning"), True)
            suite.check("F7 requestId 非空", bool(timeout.get("requestId")), timeout.get("requestId"))
            suite.equal("F7 timeoutSeconds", timeout.get("timeoutSeconds"), 0.6)
            suite.check("F7 带 note 说明不是失败", "不是失败" in str(timeout.get("note")), timeout.get("note"))

            # F8 长任务自动抬高超时
            raised = await call(
                session, "eda_execute", code="await eda.pcb_Document.autoRouting();"
            )
            suite.equal("F8 timeoutAutoRaised=true", raised.get("timeoutAutoRaised"), True)
            suite.equal("F8 超时抬到 300s", raised.get("timeoutSeconds"), 300.0)

            # F9 慢接口静态提示
            warned = await call(session, "eda_execute", code="return await eda.pcb_PrimitiveLine.getAll();")
            suite.check(
                "F9 对 getAll() 给出性能提示",
                any("getAllPrimitiveId" in item for item in warned.get("warnings", [])),
                warned.get("warnings"),
            )

            # F10 批量执行
            batch = await call(
                session,
                "eda_execute_batch",
                steps=[{"name": "一", "code": "return 1;"}, {"name": "二", "code": "return 2;"}],
            )
            suite.equal("F10 批量 ok", batch.get("ok"), True)
            suite.equal("F10 executed=2", batch.get("executed"), 2)
            suite.equal("F10 failed=0", batch.get("failed"), 0)
            suite.equal("F10 每步结果保真", batch["results"][1].get("result", {}).get("echo"), "return 2;")

            stopped = await call(
                session,
                "eda_execute_batch",
                steps=[{"code": M_ERROR}, {"code": "return 'never';"}],
                stop_on_error=True,
            )
            suite.equal("F10 stop_on_error 中断", stopped.get("aborted"), True)
            suite.equal("F10 中断后只执行 1 步", stopped.get("executed"), 1)

            invalid = await call(
                session,
                "eda_execute_batch",
                steps=[{"code": 123}, {"code": "return 1;"}],
            )
            suite.equal("F10 非法步骤被标记失败", invalid.get("failed"), 1)
            suite.equal("F10 非法步骤不中断后续", invalid.get("executed"), 2)

            empty = await call(session, "eda_execute_batch", steps=[])
            suite.equal("F10 空 steps 报错", empty.get("ok"), False)

            per_step = await call(
                session,
                "eda_execute_batch",
                steps=[{"code": M_SLOW + "2", "timeout_seconds": 0.5}],
            )
            suite.equal("F10 单步超时生效", per_step["results"][0].get("timeout"), True)

            # F11 ping
            ping = await call(session, "eda_ping")
            suite.equal("F11 ping ok", ping.get("ok"), True)
            suite.check("F11 往返耗时存在", isinstance(ping.get("roundTripMs"), (int, float)))
            await asyncio.sleep(0.5)
            suite.check(
                "F11 服务端应答了扩展心跳 ping（扩展不会误判断线）",
                eda.pongs >= 1,
                {"pings": eda.pings, "pongs": eda.pongs},
            )

            # F12 选择窗口
            selected = await call(session, "eda_select_window", window_id="win-MCP")
            suite.equal("F12 选择窗口成功", selected.get("ok"), True)
            unknown = await call(session, "eda_select_window", window_id="ghost")
            suite.equal("F12 未知窗口被拒绝", unknown.get("ok"), False)

            # F13 显式窗口不存在：MCP 层必须报错且不重试到别的窗口
            ghost = await call(session, "eda_execute", code="return 'ghost';", window_id="ghost")
            suite.equal("F13 显式窗口不存在 -> ok=false", ghost.get("ok"), False)
            suite.equal("F13 delivered=false", ghost.get("delivered"), False)
            suite.check(
                "F13 给出可用窗口", "win-MCP" in str(ghost.get("availableWindows")), ghost.get("availableWindows")
            )
            suite.check("F13 活动窗口没有收到代码", "return 'ghost';" not in eda.codes)

            # F14 检索
            search = await call(session, "eda_api_search", query="getCurrentProjectInfo")
            suite.check("F14 类方法可检索", bool(search.get("matches")), search.get("matches"))
            format_search = await call(session, "eda_api_search", query="TMSchComponent")
            suite.check(
                "F14 格式文档可检索（此前是 0 命中）",
                any(hit["path"].startswith("guide/") for hit in format_search.get("matches", [])),
                format_search.get("matches"),
            )
            padnet = await call(session, "eda_api_search", query="PAD_NET")
            suite.check(
                "F14 PAD_NET 可检索",
                any("guide/cli.md" == hit["path"] or hit["path"].startswith("format/") for hit in padnet.get("matches", [])),
                padnet.get("matches"),
            )
            suite.equal(
                "F14 索引文件总数与磁盘一致",
                search.get("indexedFiles"),
                len(index.files()),
            )
            suite.equal("F14 单次检索扫描全部文件", search.get("scannedFiles"), len(index.files()))
            bad = await call(session, "eda_api_search", query="   ")
            suite.equal("F14 空查询报错", bad.get("ok"), False)
            capped = await call(session, "eda_api_search", query="create", limit=999)
            suite.check("F14 limit 被收敛到 50", len(capped.get("matches", [])) <= 50)

            # F15 文档读取
            doc = await call(session, "eda_api_doc", path="references/classes/DMT_Project.md")
            suite.equal("F15 读取类文档 ok", doc.get("ok"), True)
            suite.check("F15 带行号", "getCurrentProjectInfo" in str(doc.get("text")), doc.get("path"))
            page1 = await call(session, "eda_api_doc", path="guide/cli.md", offset=0, limit=5)
            suite.equal("F15 翻页 returned=5", page1.get("returned"), 5)
            suite.equal("F15 hasMore", page1.get("hasMore"), True)
            traversal = await call(session, "eda_api_doc", path="../../Windows/win.ini")
            suite.equal("F15 路径穿越被拒绝", traversal.get("ok"), False)
            absolute = await call(session, "eda_api_doc", path="C:/Windows/win.ini")
            suite.equal("F15 绝对路径被拒绝", absolute.get("ok"), False)
            missing = await call(session, "eda_api_doc", path="references/classes/NOPE.md")
            suite.equal("F15 不存在文件报错", missing.get("ok"), False)

            # F18 枚举表（网关作用域没有枚举符号，只能由 MCP 提供）
            enum_all = await call(session, "eda_enum")
            suite.equal("F18 枚举可整体列出", enum_all.get("ok"), True)
            suite.check(
                "F18 枚举数量与文档一致",
                enum_all.get("count") == len(list((root / "references" / "enums").glob("*.md"))),
                enum_all.get("count"),
            )
            suite.check(
                "F18 列表包含 EPCB_LayerId", "EPCB_LayerId" in (enum_all.get("names") or [])
            )
            layer_enum = await call(session, "eda_enum", name="EPCB_LayerId")
            suite.equal("F18 查具体枚举 ok", layer_enum.get("ok"), True)
            members = layer_enum.get("members") or {}
            suite.equal("F18 TOP=1", members.get("TOP"), 1)
            suite.equal("F18 BOARD_OUTLINE=11", members.get("BOARD_OUTLINE"), 11)
            suite.check("F18 给出可直接粘贴的 JS", "const EPCB_LayerId" in str(layer_enum.get("js")), layer_enum.get("js"))
            ci = await call(session, "eda_enum", name="epcb_layerid")
            suite.equal("F18 枚举名大小写不敏感", ci.get("name"), "EPCB_LayerId")
            unknown_enum = await call(session, "eda_enum", name="ENOT_A_REAL_ENUM")
            suite.equal("F18 未知枚举报错", unknown_enum.get("ok"), False)

            # F19 枚举注入：让"用枚举写"的代码在网关里也能跑
            before = len(eda.codes)
            injected = await call(session, "eda_execute", code="return EPCB_LayerId.TOP;")
            suite.equal("F19 注入后执行成功", injected.get("ok"), True)
            suite.equal("F19 上报注入项", injected.get("injectedEnums"), ["EPCB_LayerId"])
            wire = eda.codes[before] if len(eda.codes) > before else ""
            suite.check("F19 前导定义了枚举", "const EPCB_LayerId" in wire, wire[:120])
            suite.check("F19 原始代码完整保留在末尾", wire.endswith("return EPCB_LayerId.TOP;"), wire[-80:])
            suite.check(
                "F19 前导里的取值正确",
                '"TOP": 1' in wire and '"BOARD_OUTLINE": 11' in wire,
                wire[:200],
            )

            before = len(eda.codes)
            no_inject = await call(
                session,
                "eda_execute",
                code="return EPCB_LayerId.TOP;",
                inject_enums=False,
            )
            wire = eda.codes[before] if len(eda.codes) > before else ""
            suite.equal("F19 关掉注入时代码逐字节不变", wire, "return EPCB_LayerId.TOP;")
            suite.check(
                "F19 关掉注入时给出 ReferenceError 警告",
                any("ReferenceError" in str(item) for item in no_inject.get("warnings", [])),
                no_inject.get("warnings"),
            )

            # 用户自己声明了同名变量时不得重复注入
            before = len(eda.codes)
            declared = await call(
                session,
                "eda_execute",
                code="const EPCB_LayerId = {TOP: 9}; return EPCB_LayerId.TOP;",
            )
            wire = eda.codes[before] if len(eda.codes) > before else ""
            suite.equal(
                "F19 用户自己声明同名变量时不注入",
                wire,
                "const EPCB_LayerId = {TOP: 9}; return EPCB_LayerId.TOP;",
            )
            suite.check("F19 未声明注入项", "injectedEnums" not in declared, declared.get("injectedEnums"))

            # 无枚举引用的代码必须逐字节原样（"原样转发"的底线）
            before = len(eda.codes)
            plain = await call(session, "eda_execute", code="return 1 + 1;")
            suite.equal("F19 无枚举时不改代码", eda.codes[before], "return 1 + 1;")
            suite.check("F19 无枚举时无注入项", "injectedEnums" not in plain)

            # F20 批量执行同样支持枚举注入
            batch_enum = await call(
                session,
                "eda_execute_batch",
                steps=[{"code": "return EPCB_LayerId.BOARD_OUTLINE;"}],
            )
            suite.equal("F20 批量执行成功", batch_enum.get("ok"), True)
            suite.equal(
                "F20 批量行上报注入项",
                batch_enum["results"][0].get("injectedEnums"),
                ["EPCB_LayerId"],
            )

            # F21 批量里显式关掉注入时，必须把"会抛 ReferenceError"警告带回来
            batch_warn = await call(
                session,
                "eda_execute_batch",
                steps=[{"code": "return EPCB_LayerId.TOP;", "inject_enums": False}],
            )
            row_warnings = batch_warn["results"][0].get("warnings") or []
            suite.check(
                "F21 批量步骤关掉注入时给出 ReferenceError 警告",
                any("ReferenceError" in str(item) for item in row_warnings),
                row_warnings,
            )

            # F22 分批自动布线：切片数量、keep 模式、检查点保存、断点信息
            route_nets = [f"NET{i}" for i in range(1, 12)]  # 11 个网络
            before = len(eda.codes)
            routed = await call(
                session, "eda_pcb_autoroute", nets=route_nets, chunk_size=5
            )
            chunk_codes = eda.codes[before:]
            suite.equal("F22 分批布线 ok", routed.get("ok"), True)
            suite.equal("F22 切片数 = ceil(11/5)", len(routed.get("chunks") or []), 3)
            suite.equal("F22 实际执行 3 次", len(chunk_codes), 3)
            suite.equal("F22 全部网络进入 done", len(routed.get("done") or []), 11)
            suite.equal("F22 无失败网络", routed.get("failedNets"), [])
            suite.equal("F22 无剩余网络", routed.get("remainingNets"), [])
            suite.check(
                "F22 每片都用 keep 模式（不清空已有布线）",
                all("existingPrimitiveMode: 'keep'" in code for code in chunk_codes),
                chunk_codes[:1],
            )
            suite.check(
                "F22 每片后保存检查点",
                all("eda.pcb_Document.save()" in code for code in chunk_codes),
                chunk_codes[:1],
            )
            suite.check(
                "F22 默认只布 Top/Bottom（内层留给平面）",
                all("layers: [1, 2]" in code for code in chunk_codes),
                chunk_codes[:1],
            )
            suite.check(
                "F22 切片内容与网络顺序一致",
                '"NET1"' in chunk_codes[0] and '"NET5"' in chunk_codes[0]
                and '"NET6"' in chunk_codes[1] and '"NET11"' in chunk_codes[2],
                chunk_codes,
            )

            # F23 参数边界：空网络、超大 chunk、关掉检查点、自定义层
            empty = await call(session, "eda_pcb_autoroute", nets=[])
            suite.equal("F23 空网络列表直接报错", empty.get("ok"), False)

            before = len(eda.codes)
            one_chunk = await call(
                session, "eda_pcb_autoroute", nets=["A", "B", "A"], chunk_size=999
            )
            suite.equal("F23 chunk 上限收敛后只跑 1 片", len(one_chunk.get("chunks") or []), 1)
            suite.check(
                "F23 网络去重（A 只出现一次）",
                eda.codes[before].count('"A"') == 1,
                eda.codes[before],
            )

            before = len(eda.codes)
            no_save = await call(
                session,
                "eda_pcb_autoroute",
                nets=["C"],
                save_between_chunks=False,
                layers=[1],
            )
            suite.equal("F23 单网络单切片 ok", no_save.get("ok"), True)
            suite.check(
                "F23 关掉检查点后代码里没有 save()",
                "eda.pcb_Document.save()" not in eda.codes[before],
                eda.codes[before],
            )
            suite.check(
                "F23 自定义 layers 生效", "layers: [1]" in eda.codes[before], eda.codes[before]
            )

            # F16 已送达的失败不得自动重试（重复器件风险）
            # 判别方法：让 EDA 在收到代码后立刻断开，同时 0.15s 后以同一 windowId
            # 重连。若 MCP 自动重发（旧行为：0.3s 后重试），新连接会**再收到一次**
            # 同样的代码 —— 在真实 EDA 里就是第二个同位号、同坐标的器件。
            dropped_task = asyncio.create_task(
                call(session, "eda_execute", code=M_DROP + " createComponent();")
            )
            await asyncio.sleep(0.15)
            reconnect = await FakeEda(port, "win-MCP").start()
            await wait_connected(BridgeClient(host=HOST, port=port), expect=1)
            dropped = await asyncio.wait_for(dropped_task, timeout=30)
            suite.equal("F16 断开时 ok=false", dropped.get("ok"), False)
            suite.equal("F16 delivered=true", dropped.get("delivered"), True)
            suite.equal("F16 retrySafe=false", dropped.get("retrySafe"), False)
            await asyncio.sleep(1.5)
            suite.equal(
                "F16 没有自动重发（新连接收到 0 次）",
                sum(1 for code in reconnect.codes if M_DROP in code),
                0,
            )
            suite.equal(
                "F16 原连接只收到 1 次",
                sum(1 for code in eda.codes if M_DROP in code),
                1,
            )

            # F17 断开后重新接入仍可工作
            await eda.stop()
            await reconnect.stop()
            again = await FakeEda(port, "win-MCP-2").start()
            await wait_connected(BridgeClient(host=HOST, port=port), expect=1)
            recovered = await call(session, "eda_execute", code="return 'recovered';")
            suite.equal("F17 恢复后执行成功", recovered.get("ok"), True)
            await again.stop()


async def section_f2(suite: Suite, port: int, python: Path, root: Path) -> None:
    """未送达的重试：没有窗口时发起，1 秒后窗口出现，应自动成功且只执行一次。"""
    suite.begin("F2 未送达时可安全重试（MCP 子进程）")
    from mcp import StdioServerParameters

    params = StdioServerParameters(
        command=str(python),
        args=["-m", "easyeda_mcp", "--host", HOST, "--log-level", "WARNING"],
        cwd=str(root),
    )
    async with _open_stdio_compat(params) as (read_stream, write_stream):
        async with _open_session_compat(read_stream, write_stream) as session:
            await session.initialize()
            task = asyncio.create_task(
                call(session, "eda_execute", code="return 'late-window';")
            )
            await asyncio.sleep(1.0)
            eda = await FakeEda(port, "win-retry").start()
            result = await asyncio.wait_for(task, timeout=30)
            suite.equal("F2 迟到的窗口让执行自动成功", result.get("ok"), True)
            suite.equal(
                "F2 代码只执行一次",
                sum(1 for code in eda.codes if code == "return 'late-window';"),
                1,
            )
            await eda.stop()


async def section_f3(suite: Suite, bridge_port: int, python: Path, root: Path) -> None:
    """同时存在两个桥接实例时，MCP 不能因为"当前没窗口"就跑到另一个实例上去。

    真实踩到过的坑：``_call_with_reconnect`` 原来在遇到"没有 EDA 窗口"时会重新扫描
    49620-49629，而 ``discover`` 返回的是**第一个**应答的桥接。如果端口范围内还跑着
    另一个桥接（以及另一台 EDA），代码就会被静默地送过去执行。

    构造方式：让"别人"的桥接占住范围里最靠前的端口（带一个 EDA），MCP 用 ``--port``
    显式绑到后面的端口（自己暂时没有 EDA）。修复前，MCP 会把代码送到别人的 EDA 上。
    """
    suite.begin("F3 多桥接实例不串台（MCP 子进程）")
    from mcp import StdioServerParameters

    _ = await wait_connected(BridgeClient(host=HOST, port=bridge_port), expect=0)
    other = await FakeEda(bridge_port, "win-other").start()
    await wait_connected(BridgeClient(host=HOST, port=bridge_port), expect=1)

    candidates = [
        p
        for p in range(EXT_PORT_START, EXT_PORT_END + 1)
        if p != bridge_port and _port_free(p)
    ]
    if not candidates:
        suite.check("F3 需要一个额外空闲端口", False, bridge_port)
        await other.stop()
        return
    port_a = candidates[0]

    params = StdioServerParameters(
        command=str(python),
        args=[
            "-m",
            "easyeda_mcp",
            "--host",
            HOST,
            "--port",
            str(port_a),
            "--log-level",
            "WARNING",
        ],
        cwd=str(root),
    )
    try:
        async with _open_stdio_compat(params) as (read_stream, write_stream):
            async with _open_session_compat(read_stream, write_stream) as session:
                await session.initialize()
                status = await call(session, "eda_status")
                suite.equal(
                    "F3 MCP 使用自己指定的端口",
                    status.get("bridge", {}).get("port"),
                    port_a,
                )
                result = await call(
                    session, "eda_execute", code="return 'must-not-leak';"
                )
                suite.equal("F3 自己没有窗口时如实失败", result.get("ok"), False)
                suite.equal("F3 delivered=false（可安全重试）", result.get("delivered"), False)
                suite.equal(
                    "F3 另一个桥接上的 EDA 一次都没收到代码",
                    sum(1 for code in other.codes if "must-not-leak" in code),
                    0,
                )

                mine = await FakeEda(port_a, "win-mine").start()
                await wait_connected(BridgeClient(host=HOST, port=port_a), expect=1)
                ok = await call(session, "eda_execute", code="return 'mine';")
                suite.equal("F3 自己的窗口接上后可执行", ok.get("ok"), True)
                suite.equal(
                    "F3 结果来自自己的窗口",
                    (ok.get("result") or {}).get("windowId"),
                    "win-mine",
                )
                await mine.stop()
    finally:
        await other.stop()


# ─── G. 文档检索 ──────────────────────────────────────────────────────


def section_g(suite: Suite, index: DocIndex) -> None:
    suite.begin("G 文档检索")
    cases: list[tuple[str, str]] = [
        ("createBoard", "references"),
        ("EPCB_LayerId", "references"),
        ("rebuildCopperRegion", "references"),
        ("TMSchComponent", "guide"),
        ("PAD_NET", "format"),
        ("TPcbPoly", "guide"),
        ("getProjectsPaths", "guide"),
    ]
    for query, expected_prefix in cases:
        hits = index.search(query, limit=20)
        paths = [hit["path"] for hit in hits["matches"]]
        suite.check(
            f"G1 「{query}」有命中",
            bool(paths),
            hits["matches"][:2],
        )
        suite.check(
            f"G1 「{query}」命中落在 {expected_prefix}/",
            any(path.startswith(expected_prefix + "/") for path in paths) or not paths,
            paths[:3],
        )

    started = time.perf_counter()
    index.search("permission")
    elapsed = time.perf_counter() - started
    suite.check("G2 全库检索耗时 < 3s", elapsed < 3.0, round(elapsed, 3))

    none = index.search("zzzz-not-exist-zzzz")
    suite.equal("G3 无命中时返回空", none["matches"], [])

    try:
        index.search("   ")
        suite.fail("G4 空 query 抛 DocError", "没有抛异常")
    except DocError:
        suite.check("G4 空 query 抛 DocError", True)

    limited = index.search("create", limit=1)
    suite.check("G5 limit 生效", len(limited["matches"]) <= 1, len(limited["matches"]))

    hit = index.search("getCurrentProjectInfo", limit=5)["matches"][0]
    page = index.read(hit["path"], offset=max(0, hit["line"] - 2), limit=3)
    suite.check(
        "G6 命中行号可直接定位到文档内容",
        "getCurrentProjectInfo" in page["text"],
        page["text"][:120],
    )


# ─── I. 缺陷回归（并发 / 参数 / 资源边界） ─────────────────────────────


class _GhostSocket:
    """伪装"已关闭但仍留在 ``_eda_clients`` 里"的 socket（只需要 .closed）。"""

    closed = True


async def section_i(
    suite: Suite,
    port: int,
    bridge: BridgeServer,
    client: BridgeClient,
    root: Path,
    index: DocIndex,
    python: Path,
) -> None:
    suite.begin("I 缺陷回归（并发 / 参数 / 资源边界）")

    # I2 死连接残留：/eda-windows 与 /health 的口径必须一致
    bridge._eda_clients["ghost"] = _GhostSocket()  # type: ignore[assignment]
    try:
        windows = await client.list_windows()
        health = await client.health()
        suite.equal(
            "I2 /eda-windows 与 /health 的窗口数一致",
            windows.get("count"),
            health.get("edaWindowCount"),
        )
        suite.check(
            "I2 已关闭的窗口不出现在列表里",
            all(item.get("windowId") != "ghost" for item in windows.get("windows", [])),
            windows.get("windows"),
        )
    finally:
        bridge._eda_clients.pop("ghost", None)

    # I3 非法 timeout 必须 400（NaN 会让 wait_for 的行为未定义）
    params_eda = await FakeEda(port, "win-params").start()
    await wait_connected(client, expect=1)
    try:
        async with aiohttp.ClientSession() as session:
            for label, body in [
                ("NaN", '{"code":"return 1;","timeout":NaN}'),
                ("Infinity", '{"code":"return 1;","timeout":Infinity}'),
                ("布尔", '{"code":"return 1;","timeout":true}'),
                ("字符串", '{"code":"return 1;","timeout":"2"}'),
            ]:
                async with session.post(
                    f"http://{HOST}:{port}/execute",
                    data=body.encode(),
                    headers={"Content-Type": "application/json"},
                ) as resp:
                    status = resp.status
                suite.equal(f"I3 非法 timeout（{label}）被拒绝", status, 400)
    finally:
        await params_eda.stop()
        await wait_connected(client, expect=0)

    # I4 WS 单帧上限（0 = 无限制，可被本机进程拖爆内存）
    suite.equal(
        "I4 WS 单帧上限设为 100MB（与上游 Node 默认一致）",
        getattr(BridgeServer, "WS_MAX_MSG_SIZE", None),
        100 * 1024 * 1024,
    )

    # I5 超长 windowId 不应无界进入状态与响应
    huge = await FakeEda(port, "w" * 10000).start()
    await asyncio.sleep(0.3)
    windows = await client.list_windows()
    lengths = [len(item.get("windowId", "")) for item in windows.get("windows", [])]
    suite.check("I5 超长 windowId 不被接受", all(length <= 256 for length in lengths), lengths)
    await huge.stop()
    await wait_connected(client, expect=0)

    # I6 文档参数容错：工具层应返回错误，而不是把 ValueError 抛穿
    for label, call in [
        ("search limit", lambda: index.search("create", limit="abc")),
        ("read offset", lambda: index.read("references/_index.md", offset="abc")),
    ]:
        try:
            call()
            suite.check(f"I6 非法参数（{label}）抛 DocError", False, "没有抛异常")
        except DocError:
            suite.check(f"I6 非法参数（{label}）抛 DocError", True)
        except Exception as exc:  # noqa: BLE001
            suite.check(f"I6 非法参数（{label}）抛 DocError", False, type(exc).__name__)

    # I7 超长 query 应被拒绝（否则每次检索都要做一次超长字符串比较）
    try:
        index.search("x" * 100000)
        suite.check("I7 超长 query 被拒绝", False, "没有抛异常")
    except DocError:
        suite.check("I7 超长 query 被拒绝", True)
    except Exception as exc:  # noqa: BLE001
        suite.check("I7 超长 query 被拒绝", False, type(exc).__name__)

    # I8 枚举注入的 JS 前导必须是纯 ASCII（U+2028/U+2029 会截断字符串字面量）
    enums = EnumIndex(root)
    non_ascii = [name for name in enums.names() if not enums.js_literal(name).isascii()]
    suite.check("I8 枚举 JS 前导为纯 ASCII", not non_ascii, non_ascii[:3])

    # I9 长任务启发式不能把字符串/注释里的名字当成调用
    suite.equal(
        "I9 字符串里的 autoRouting 不抬高超时",
        _effective_timeout('return "autoRouting";', None),
        (None, False),
    )
    suite.equal(
        "I9 注释里的 autoRouting 不抬高超时",
        _effective_timeout("// autoRouting is slow\nreturn 1;", None),
        (None, False),
    )
    suite.equal(
        "I9 真实调用 autoRouting 仍抬高超时",
        _effective_timeout("await eda.pcb_Document.autoRouting();", None),
        (P.LONG_REQUEST_TIMEOUT, True),
    )

    # I11 --bridge off + --port 不得新建桥接（off 的语义是"只连已有"）
    probe_port = free_port()

    def _spawn_off() -> subprocess.Popen[bytes]:
        return subprocess.Popen(
            [
                str(python),
                "-m",
                "easyeda_mcp",
                "--bridge",
                "off",
                "--host",
                HOST,
                "--port",
                str(probe_port),
                "--transport",
                "none",
                "--log-level",
                "WARNING",
            ],
            cwd=str(root),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    child = await asyncio.to_thread(_spawn_off)
    try:
        await asyncio.sleep(2.0)
        spawned = True
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"http://{HOST}:{probe_port}/health",
                    timeout=aiohttp.ClientTimeout(total=1),
                ) as resp:
                    await resp.json(content_type=None)
        except Exception:  # noqa: BLE001
            spawned = False
        suite.check(
            "I11 --bridge off + --port 不新建桥接",
            not spawned,
            f"端口 {probe_port} 上出现了新桥接",
        )
    finally:
        await asyncio.to_thread(child.kill)
        await asyncio.to_thread(child.wait)


# ─── H. CLI ───────────────────────────────────────────────────────────


async def _run_cli(python: Path, args: list[str], timeout: float = 30.0) -> tuple[int, str, str]:
    """同步 Popen 跑一次 CLI（asyncio 子进程在沙箱下需要命名管道，会失败）。"""

    def _sync() -> tuple[int, str, str]:
        process = subprocess.Popen(
            [str(python), *args],
            cwd=str(ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            out, err = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            out, err = process.communicate()
            return -1, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")
        return (
            int(process.returncode or 0),
            out.decode("utf-8", "replace"),
            err.decode("utf-8", "replace"),
        )

    return await asyncio.to_thread(_sync)


async def section_h(suite: Suite, python: Path) -> None:
    suite.begin("H CLI")

    code, out, err = await _run_cli(python, ["-m", "easyeda_mcp", "--version"])
    suite.equal("H1 --version 退出码 0", code, 0)
    suite.check("H1 打印版本号", "easyeda-mcp" in (out + err), (out + err)[:120])

    code, out, err = await _run_cli(
        python, ["-m", "easyeda_mcp", "--bridge", "off", "--host", "127.0.0.9"]
    )
    suite.check("H2 --bridge off 找不到桥接时退出非 0", code != 0, code)
    suite.check("H2 给出可执行提示", "没有发现" in (out + err), (out + err)[:200])

    port = free_port()

    def _spawn_bridge_only() -> subprocess.Popen[bytes]:
        return subprocess.Popen(
            [
                str(python),
                "-m",
                "easyeda_mcp",
                "--transport",
                "none",
                "--port",
                str(port),
                "--log-level",
                "WARNING",
            ],
            cwd=str(ROOT),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    process = await asyncio.to_thread(_spawn_bridge_only)
    client = BridgeClient(port=port)
    ok = False
    for _ in range(60):
        try:
            payload = await client.health()
            if payload.get("service") == EXT_SERVICE_ID:
                ok = True
                break
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(0.1)
    suite.check("H3 --transport none 只跑桥接并可访问 /health", ok)
    await asyncio.to_thread(process.kill)
    await asyncio.to_thread(process.wait)

    # H4 指定的端口被占用时应报错退出（port_start==port_end）
    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    busy_port = blocker.getsockname()[1]
    try:
        code, out, err = await _run_cli(
            python,
            ["-m", "easyeda_mcp", "--transport", "none", "--port", str(busy_port)],
            timeout=20,
        )
        suite.check("H4 端口被占用时退出非 0", code != 0, code)
        suite.check("H4 错误信息可读", "错误" in (out + err) or "占用" in (out + err), (out + err)[:200])
    finally:
        blocker.close()


# ─── 主流程 ───────────────────────────────────────────────────────────


async def run(verbose: bool, report: Path | None) -> int:
    global _SDK_STDIO_OK
    root = ROOT
    suite = Suite(verbose=verbose)

    _SDK_STDIO_OK = await _probe_sdk_stdio()
    print(
        "MCP stdio 传输："
        + ("官方 mcp.client.stdio" if _SDK_STDIO_OK else "自实现（沙箱禁用 overlapped 命名管道）")
    )

    index = section_a(suite, root)

    port = await pick_bridge_port()
    print(
        f"\n桥接地址：{HOST}:{port}（验证专用回环地址，真机扩展只连 127.0.0.1，不会误入）"
    )
    bridge = BridgeServer(host=HOST, port_start=port, port_end=port)
    await bridge.start()
    client = BridgeClient(host=HOST, port=port)
    try:
        await section_b(suite, port)
        await section_c(suite, port)

        # 超时上限收敛分支：临时把上限压到 2 秒，验证 clamp 真的生效
        original_max = P.MAX_REQUEST_TIMEOUT

        async def clamp_check() -> None:
            suite.begin("B2 超时上限收敛（HTTP）")
            eda = await FakeEda(port, "win-CLAMP").start()
            await wait_connected(client, expect=1)
            try:
                try:
                    await client.execute(M_SLOW + "3", timeout=100)
                    suite.fail("B18 timeout 上限收敛", "未超时")
                except Exception as exc:  # noqa: BLE001
                    suite.equal(
                        "B18 timeout 被收敛到 MAX_REQUEST_TIMEOUT",
                        getattr(exc, "timeout_s", None),
                        2.0,
                    )
            finally:
                await eda.stop()
                await wait_connected(client, expect=0)

        P.MAX_REQUEST_TIMEOUT = 2.0
        try:
            await clamp_check()
        finally:
            P.MAX_REQUEST_TIMEOUT = original_max

        await section_d(suite, port, client)
        await section_e(suite, port, client)
        await section_f(suite, port, Path(sys.executable), root, index)
        await section_f2(suite, port, Path(sys.executable), root)
        await section_f3(suite, port, Path(sys.executable), root)
        section_g(suite, index)
        await section_i(
            suite, port, bridge, client, root, index, Path(sys.executable)
        )
        await section_h(suite, Path(sys.executable))
    finally:
        await bridge.stop()

    print("\n" + "=" * 72)
    print(f"逐分支穷举结果：通过 {suite.passed} / {suite.total}，失败 {suite.failed}")
    if suite.failed:
        print("\n失败清单：")
        for record in suite.records:
            if not record["ok"]:
                print(f"  - [{record['section']}] {record['name']} -> {record['detail']}")
    print("=" * 72)

    if report is not None:
        report.write_text(
            json.dumps(
                {
                    "python": sys.version,
                    "passed": suite.passed,
                    "failed": suite.failed,
                    "total": suite.total,
                    "records": suite.records,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"报告已写入：{report}")
    return 0 if suite.failed == 0 else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="EasyEDA MCP 逐分支穷举端到端验证")
    parser.add_argument("-v", "--verbose", action="store_true", help="打印每一条通过项")
    parser.add_argument("--report", type=Path, default=None, help="把结果写成 JSON 报告")
    args = parser.parse_args()
    print(f"解释器：{sys.executable}")
    print(f"项目根目录：{ROOT}")
    try:
        code = asyncio.run(run(args.verbose, args.report))
    except KeyboardInterrupt:
        return 130
    print(f"\n退出码：{code}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
