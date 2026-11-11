"""EasyEDA 桥接服务的 aiohttp 实现。

一个 aiohttp 应用在同一个端口上同时服务两端：

* HTTP（供 MCP 工具 / 脚本 / curl 使用）
    - ``GET  /health``              -> 服务身份与连接状态
    - ``GET  /eda-windows``         -> 已连接的 EasyEDA 窗口列表
    - ``POST /eda-windows/select``  -> 选择当前活动窗口
    - ``POST /execute``             -> 在 EasyEDA 中执行 JavaScript
* WebSocket（供 EasyEDA 专业版本身以及原始 agent 客户端使用）
    - ``/eda``   -> EasyEDA 扩展（register + execute/result/error/ping）
    - 其它任意路径（例如 ``/agent``）-> 原始 JSON 请求的 agent 客户端

报文的字段定义见 :mod:`easyeda_mcp.protocol`，与上游 Node.js 版本逐字节兼容。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
from dataclasses import dataclass
from typing import Any

from aiohttp import WSMsgType, web

from . import protocol as P
from .errors import BridgeError, EdaNotConnectedError, EdaTimeoutError

logger = logging.getLogger(__name__)

#: 所有 HTTP 响应都会带上的跨域头（浏览器里的调试页面也能直接调用）。
CORS_HEADERS: dict[str, str] = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "POST, GET, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
}


@dataclass(slots=True)
class _PendingRequest:
    """一个已发送给 EasyEDA、正在等待回复的 ``execute`` 请求。

    ``socket`` 记录请求实际发往哪一条 WebSocket。EDA 断线重连后同一个
    ``windowId`` 可能换到新连接上，只有按 socket 归属清理在途请求，才不会把
    新连接上的请求一起判死。
    """

    future: asyncio.Future[Any]
    window_id: str
    socket: web.WebSocketResponse


class BridgeServer:
    """在调用方的事件循环里运行桥接服务（HTTP 接口 + EDA WebSocket 端点）。

    典型用法::

        bridge = BridgeServer()
        port = await bridge.start()
        ...
        await bridge.stop()
    """

    #: 单条 WebSocket 消息的上限。0 表示不限制，本机任意进程都能用一条超大帧把
    #: 桥接进程的内存打满；100MB 与上游 Node 版 ``ws`` 的默认 ``maxPayload`` 一致，
    #: 足以容纳 ``getAll()`` 之类的巨型结果（实测 5MB 结果远小于此）。
    WS_MAX_MSG_SIZE = 100 * 1024 * 1024

    #: 允许的 windowId 最大长度。扩展正常发的是 UUID（36 字符）；限制长度可以
    #: 防止异常客户端用超长 ID 让状态/响应体无界膨胀。
    WINDOW_ID_MAX_LEN = 256

    def __init__(
        self,
        *,
        host: str = P.DEFAULT_HOST,
        port_start: int = P.PORT_START,
        port_end: int = P.PORT_END,
        request_timeout: float = P.DEFAULT_REQUEST_TIMEOUT,
    ) -> None:
        if port_start > port_end:
            raise ValueError("port_start 不能大于 port_end")
        self.host = host
        self.port_start = port_start
        self.port_end = port_end
        self.request_timeout = request_timeout

        self.port: int | None = None
        self._app: web.Application | None = None
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self._eda_clients: dict[str, web.WebSocketResponse] = {}
        self._pending: dict[str, _PendingRequest] = {}
        self._active_window_id: str | None = None
        # 窗口重连（EDA 长任务期间扩展会重连并换 windowId）的诊断信息
        self._eda_registrations = 0
        self._last_registration: dict[str, Any] | None = None
        self._window_history: list[dict[str, Any]] = []

    # ─── 生命周期 ───────────────────────────────────────────────────────

    @property
    def started(self) -> bool:
        """桥接服务是否已经监听端口。"""
        return self._site is not None

    @property
    def active_window_id(self) -> str | None:
        """当前活动的 EasyEDA 窗口 ID。"""
        return self._active_window_id

    @property
    def pending_requests(self) -> int:
        """尚未收到回复的请求数量。"""
        return len(self._pending)

    def connected_windows(self) -> list[str]:
        """已注册且连接仍然活着的 EasyEDA 窗口 ID 列表。"""
        return [wid for wid, ws in self._eda_clients.items() if not ws.closed]

    def _live_clients(self) -> dict[str, web.WebSocketResponse]:
        """当前仍然活着的窗口 -> socket 映射。"""
        return {wid: ws for wid, ws in self._eda_clients.items() if not ws.closed}

    def _ensure_active(self) -> str | None:
        """保证 ``active_window_id`` 指向一个仍然活着的窗口。"""
        live = self._live_clients()
        if self._active_window_id and self._active_window_id in live:
            return self._active_window_id
        self._active_window_id = next(iter(live), None)
        return self._active_window_id

    async def start(self) -> int:
        """在配置的端口范围里绑定第一个空闲端口，并返回该端口号。"""
        if self._site is not None and self.port is not None:
            return self.port

        self._app = self._build_app()
        self._runner = web.AppRunner(self._app, access_log=None)
        await self._runner.setup()

        last_error: OSError | None = None
        for port in range(self.port_start, self.port_end + 1):
            site = web.TCPSite(self._runner, self.host, port)
            try:
                await site.start()
            except OSError as exc:  # 端口被占用 -> 继续尝试下一个
                last_error = exc
                logger.debug("端口 %s 不可用：%s", port, exc)
                continue
            self._site = site
            self.port = port
            logger.info("桥接服务已监听 http://%s:%s", self.host, port)
            return port

        assert self._runner is not None
        await self._runner.cleanup()
        self._runner = None
        self._app = None
        raise BridgeError(
            f"端口范围 {self.port_start}-{self.port_end} 已全部被占用"
            f"（最后一次错误：{last_error}）"
        )

    async def stop(self) -> None:
        """关闭监听、断开 EDA 连接，并让所有在途请求立即失败。"""
        self._fail_all_pending("桥接服务正在关闭")

        for ws in list(self._eda_clients.values()):
            with contextlib.suppress(Exception):
                await ws.close(code=1001, message=b"bridge shutting down")
        self._eda_clients.clear()
        self._active_window_id = None

        runner, site = self._runner, self._site
        self._runner = None
        self._site = None
        self.port = None
        self._app = None
        if runner is not None:
            with contextlib.suppress(Exception):
                await runner.cleanup()
        if site is not None:
            logger.info("桥接服务已停止")

    # ─── HTTP 接口 ──────────────────────────────────────────────────────

    def _build_app(self) -> web.Application:
        # EasyEDA 侧生成的代码可以很长（整段批量脚本），默认 1MB 的请求体上限会
        # 直接 413（上游 Node 版没有这个限制）。这里放宽到 64MB。
        app = web.Application(
            middlewares=[self._cors_middleware], client_max_size=64 * 1024 * 1024
        )
        app.router.add_get("/health", self.handle_health)
        app.router.add_get("/eda-windows", self.handle_eda_windows)
        app.router.add_post("/eda-windows/select", self.handle_select_window)
        app.router.add_post("/execute", self.handle_execute)
        # 兜底路由：WebSocket 升级可能发往 /eda、/agent 或任何其它路径。
        app.router.add_route("*", "/{tail:.*}", self.handle_ws)
        return app

    @web.middleware
    async def _cors_middleware(
        self, request: web.Request, handler: Any
    ) -> web.StreamResponse:
        """统一添加跨域头，并直接响应浏览器的 OPTIONS 预检请求。"""
        if request.method == "OPTIONS":
            return self._json_response(None, status=204)
        response = await handler(request)
        response.headers.update(CORS_HEADERS)
        return response

    @staticmethod
    def _json_response(payload: Any, status: int = 200) -> web.Response:
        response = web.json_response(payload, status=status)
        response.headers.update(CORS_HEADERS)
        return response

    async def handle_health(self, request: web.Request) -> web.Response:
        """``GET /health``：服务身份 + 连接状态。"""
        return self._json_response(self.health_payload())

    def health_payload(self) -> dict[str, Any]:
        """构造 ``/health`` 返回的字典（字段名与 Node.js 版完全一致）。"""
        live = self._live_clients()
        self._ensure_active()
        return {
            "service": P.SERVICE_ID,
            "status": "ok",
            "edaConnected": bool(live),
            "edaWindowCount": len(live),
            "activeWindowId": self._active_window_id,
            "pendingRequests": len(self._pending),
            "edaRegistrations": self._eda_registrations,
            "lastRegistration": self._last_registration,
            "windowHistory": self._window_history[-10:],
            "timestamp": P.now_ms(),
        }

    async def handle_eda_windows(self, request: web.Request) -> web.Response:
        """``GET /eda-windows``：列出所有已连接的 EasyEDA 窗口。"""
        return self._json_response(self.windows_payload())

    def windows_payload(self) -> dict[str, Any]:
        """构造 ``/eda-windows`` 返回的字典。

        只列仍然活着的连接：``_eda_clients`` 里可能残留刚关闭、协程还没跑完
        ``finally`` 的 socket。把它们算进 ``count`` 会让 ``/eda-windows`` 与
        ``/health`` 的口径不一致（一个说 1 个窗口、一个说 0 个），MCP 侧据此
        做出的"是否已连接"判断就会自相矛盾。
        """
        live = self._live_clients()
        self._ensure_active()
        windows = [
            {
                "windowId": window_id,
                "connected": True,
                "active": window_id == self._active_window_id,
            }
            for window_id in live
        ]
        return {
            "windows": windows,
            "activeWindowId": self._active_window_id,
            "count": len(windows),
        }

    async def handle_select_window(self, request: web.Request) -> web.Response:
        """``POST /eda-windows/select``：把某个窗口设为默认目标。"""
        try:
            payload = await request.json()
        except (json.JSONDecodeError, ValueError):
            return self._json_response({"error": "请求体不是合法的 JSON"}, status=400)

        window_id = payload.get("windowId") if isinstance(payload, dict) else None
        if not isinstance(window_id, str) or window_id not in self._live_clients():
            return self._json_response(
                {"error": f'未找到 EasyEDA 窗口 "{window_id}"'}, status=404
            )

        self._active_window_id = window_id
        logger.info("当前活动 EasyEDA 窗口已切换为 %s", window_id)
        return self._json_response({"success": True, "activeWindowId": window_id})

    async def handle_execute(self, request: web.Request) -> web.Response:
        """``POST /execute``：在 EasyEDA 中执行一段 JavaScript。"""
        try:
            payload = await request.json()
        except (json.JSONDecodeError, ValueError):
            return self._json_response({"error": "请求体不是合法的 JSON"}, status=400)

        if not isinstance(payload, dict):
            return self._json_response({"error": "请求体不是合法的 JSON"}, status=400)

        code = payload.get("code")
        window_id = payload.get("windowId")
        timeout = payload.get("timeout")
        if not isinstance(code, str) or not code:
            return self._json_response(
                {"error": '缺少 "code" 字段（应为非空字符串）'}, status=400
            )
        if window_id is not None and not isinstance(window_id, str):
            return self._json_response(
                {"error": '"windowId" 必须为字符串'}, status=400
            )

        # 允许调用方为单次请求指定超时（秒）。长任务（布线 / 自动布局 /
        # Gerber 导出）动辄 1~5 分钟，用默认 30 秒必然"看起来失败"。
        #
        # 校验必须严格：JSON 允许 NaN/Infinity 字面量，`float(True)` 又等于 1.0，
        # 一旦放行 `asyncio.wait_for(..., nan)`，定时器的行为是未定义的（可能立刻
        # 超时、也可能永不到期），调用方会拿到一条无法复现的"超时"。
        if timeout is not None:
            if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
                return self._json_response(
                    {"error": '"timeout" 必须是数字（单位：秒）'}, status=400
                )
            timeout_value = float(timeout)
            if not math.isfinite(timeout_value):
                return self._json_response(
                    {"error": '"timeout" 必须是有限数字'}, status=400
                )
            if timeout_value <= 0:
                return self._json_response(
                    {"error": '"timeout" 必须大于 0'}, status=400
                )
            timeout = min(timeout_value, P.MAX_REQUEST_TIMEOUT)

        try:
            target = self._resolve_window(window_id)
            result = await self.execute(code, window_id=target, timeout=timeout)
        except EdaTimeoutError as exc:
            # 超时单独处理：明确告诉调用方"任务还在 EasyEDA 里跑"，
            # 避免它把超时当成失败并重复提交同一个任务。
            return self._json_response(
                {"success": False, "error": str(exc), **exc.to_payload()},
                status=exc.status_code,
            )
        except BridgeError as exc:
            payload: dict[str, Any] = {"success": False, "error": str(exc)}
            if isinstance(exc, EdaNotConnectedError):
                # delivered=false 表示请求从未送达 EDA，调用方可以安全重试。
                payload.update(exc.to_payload())
            payload.update(exc.details)
            return self._json_response(payload, status=exc.status_code)

        return self._json_response(
            {"success": True, "result": result, "windowId": target}
        )

    # ─── WebSocket 处理 ─────────────────────────────────────────────────

    async def handle_ws(self, request: web.Request) -> web.StreamResponse:
        """接受 WebSocket 升级请求，并按路径区分 EDA 客户端与 agent 客户端。"""
        if request.headers.get("Upgrade", "").lower() != "websocket":
            return self._json_response({"error": "未找到该接口"}, status=404)

        ws = web.WebSocketResponse(
            autoping=True,
            autoclose=True,
            # 单帧上限：不能设 0（无限制），否则本机任意进程一条超大消息就能把
            # 桥接进程的内存打满；100MB 与上游 Node 默认值一致。
            max_msg_size=self.WS_MAX_MSG_SIZE,
        )
        await ws.prepare(request)

        client_type = "eda" if request.path == "/eda" else "agent"
        logger.info(
            "[WS] 新的 %s 连接，来自 %s", client_type, request.remote or "未知地址"
        )
        await self._safe_send(ws, P.handshake_message(client_type))

        try:
            if client_type == "eda":
                await self._serve_eda(ws)
            else:
                await self._serve_agent(ws)
        finally:
            if not ws.closed:
                with contextlib.suppress(Exception):
                    await ws.close()
        return ws

    async def _serve_eda(self, ws: web.WebSocketResponse) -> None:
        """服务一个 EasyEDA 扩展连接（register / result / error / ping）。

        窗口 ID 的归属必须按 socket 判断：新版扩展把 windowId 存在
        ``sessionStorage`` 里，同一标签页重连后 ID 不变。如果还按老写法在
        ``finally`` 里无条件 ``pop(windowId)``，旧连接关闭时会把刚注册的新连接
        一起摘掉——此后桥接认为"没有窗口"，而扩展自己以为已连上，双方都收不到
        对方的消息，只能等超时。
        """
        window_id: str | None = None
        try:
            async for message in ws:
                payload = self._decode(message)
                if payload is None:
                    continue

                if payload.get("type") == P.MSG_REGISTER and payload.get("windowId"):
                    new_window_id = str(payload["windowId"])
                    if len(new_window_id) > self.WINDOW_ID_MAX_LEN or any(
                        ch in new_window_id for ch in "\r\n\0"
                    ):
                        # 正常扩展发的是 UUID。超长/带控制字符的 ID 只会污染
                        # 状态与日志（换行还能伪造日志行），直接拒绝注册。
                        logger.warning(
                            "[WS] 拒绝异常 windowId（长度 %s，含控制字符=%s）",
                            len(new_window_id),
                            any(ch in new_window_id for ch in "\r\n\0"),
                        )
                        continue
                    # 同一个 socket 换 ID（旧版扩展每次重连都发新 UUID）
                    if window_id is not None and window_id != new_window_id:
                        if self._eda_clients.get(window_id) is ws:
                            self._eda_clients.pop(window_id, None)
                        self._window_history.append(
                            {
                                "windowId": window_id,
                                "event": "superseded",
                                "nextWindowId": new_window_id,
                                "at": P.now_ms(),
                            }
                        )
                        del self._window_history[:-20]
                        # 旧 ID 上没有结果可等了，直接让它失败并说明原因
                        self._fail_pending_for_window(
                            window_id,
                            f'EasyEDA 窗口 "{window_id}" 已改名为 "{new_window_id}"',
                        )
                    window_id = new_window_id
                    self._eda_clients[window_id] = ws
                    self._eda_registrations += 1
                    self._last_registration = {
                        "windowId": window_id,
                        "serial": self._eda_registrations,
                        "at": P.now_ms(),
                    }
                    self._window_history.append(
                        {
                            "windowId": window_id,
                            "serial": self._eda_registrations,
                            "event": "register",
                            "at": P.now_ms(),
                        }
                    )
                    del self._window_history[:-20]
                    # 第一个窗口或尚无活动窗口时自动选中
                    if not self._active_window_id or self._active_window_id not in self._live_clients():
                        self._active_window_id = window_id
                    logger.info(
                        "[WS] EasyEDA 窗口已注册：%s（第 %s 次注册），当前总数：%s",
                        window_id,
                        self._eda_registrations,
                        len(self._live_clients()),
                    )
                    continue

                await self._handle_eda_message(
                    payload, window_id or P.UNREGISTERED_WINDOW_ID, ws
                )
        finally:
            if window_id is not None:
                # 只有仍由本 socket 持有的注册才回收；被新连接接管的同名窗口不动。
                superseded = self._eda_clients.get(window_id) is not ws
                if not superseded:
                    self._eda_clients.pop(window_id, None)
                    if self._active_window_id == window_id:
                        self._active_window_id = None
                self._ensure_active()
                self._fail_pending_for_socket(
                    ws,
                    f'EasyEDA 窗口 "{window_id}" 已断开连接'
                    if not superseded
                    else f'EasyEDA 窗口 "{window_id}" 的旧连接已断开（已由新连接接管）',
                )
                self._window_history.append(
                    {
                        "windowId": window_id,
                        "event": "disconnect",
                        "superseded": superseded,
                        "closeCode": ws.close_code,
                        "at": P.now_ms(),
                    }
                )
                del self._window_history[:-20]
                logger.info(
                    "[WS] EasyEDA 连接已断开：%s（close_code=%s，被接管=%s），剩余：%s，当前活动窗口：%s",
                    window_id,
                    ws.close_code,
                    superseded,
                    len(self._live_clients()),
                    self._active_window_id,
                )

    async def _handle_eda_message(
        self, payload: dict[str, Any], window_id: str, ws: web.WebSocketResponse
    ) -> None:
        """处理一条来自 EasyEDA 的报文。"""
        msg_type = payload.get("type")

        if msg_type == P.MSG_PING:
            logger.debug("[WS] 收到 %s 的 ping，回复 pong", window_id)
            await self._safe_send(ws, P.pong_message(payload.get("id")))
            return

        if msg_type == P.MSG_PONG:
            logger.debug("[EDA] 收到窗口 %s 的 pong", window_id)
            return

        if msg_type in (P.MSG_RESULT, P.MSG_ERROR):
            request_id = payload.get("id")
            pending = self._pending.get(request_id) if isinstance(request_id, str) else None
            if pending is None:
                logger.debug("[EDA] 忽略未知请求 %r 的回复", request_id)
                return
            if pending.future.done():
                return
            if msg_type == P.MSG_RESULT:
                pending.future.set_result(payload.get("result"))
            else:
                pending.future.set_exception(
                    BridgeError(str(payload.get("error") or "EasyEDA 返回了未知错误"))
                )
            return

        logger.debug("[EDA] 来自 %s 的未知报文类型 %r", window_id, msg_type)

    async def _serve_agent(self, ws: web.WebSocketResponse) -> None:
        """服务一个原始 WebSocket agent 客户端（execute / ping）。"""
        try:
            async for message in ws:
                payload = self._decode(message)
                if payload is None:
                    continue

                msg_type = payload.get("type")
                if msg_type == P.MSG_EXECUTE:
                    request_id = payload.get("id")
                    code = payload.get("code")
                    if not isinstance(code, str):
                        await self._safe_send(
                            ws, P.error_message(request_id, '缺少 "code" 字段')
                        )
                        continue
                    try:
                        window_id = payload.get("windowId")
                        target = self._resolve_window(
                            window_id if isinstance(window_id, str) else None
                        )
                        result = await self.execute(code, window_id=target)
                    except BridgeError as exc:
                        await self._safe_send(ws, P.error_message(request_id, str(exc)))
                        continue
                    await self._safe_send(ws, P.result_message(request_id, result))
                elif msg_type == P.MSG_PING:
                    await self._safe_send(ws, P.pong_message(payload.get("id")))
        finally:
            logger.info("[WS] agent 客户端已断开")

    @staticmethod
    def _decode(message: Any) -> dict[str, Any] | None:
        """把 WebSocket 消息解析成字典，失败时返回 None。"""
        if message.type not in (WSMsgType.TEXT, WSMsgType.BINARY):
            return None
        try:
            raw = message.data
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", errors="replace")
            payload = json.loads(raw)
        except (ValueError, UnicodeDecodeError) as exc:
            logger.warning("[WS] 报文解析失败：%s", exc)
            return None
        return payload if isinstance(payload, dict) else None

    @staticmethod
    async def _safe_send(ws: web.WebSocketResponse, payload: dict[str, Any]) -> bool:
        """发送 JSON 报文；连接已关闭时返回 False 而不是抛异常。"""
        if ws.closed:
            return False
        try:
            await ws.send_json(payload)
        except (ConnectionResetError, RuntimeError) as exc:
            logger.warning("[WS] 报文发送失败：%s", exc)
            return False
        return True

    # ─── 核心：在 EasyEDA 中执行代码 ─────────────────────────────────────

    def _resolve_window(self, window_id: str | None) -> str:
        """确定目标 EasyEDA 窗口，不可用时抛出 :class:`EdaNotConnectedError`。

        两条原则：

        1. **没指定窗口**时才自动挑（当前活动窗口 / 唯一在线窗口）。
        2. **显式指定的窗口不在线**时直接报错，绝不悄悄改投到另一个窗口。
           多窗口场景下静默改投会把代码跑到另一个工程的图纸上，这种"看起来
           成功"的错误比一次明确的失败危险得多。调用方拿到
           ``availableWindows`` 后可以自行决定改投哪个。

        注意这里抛出的错误 ``delivered=False``：请求尚未送达任何 EDA，重试安全。
        """
        live = self._live_clients()
        available = list(live)

        if window_id:
            if window_id in live:
                self._active_window_id = window_id
                return window_id
            raise EdaNotConnectedError(
                f'EasyEDA 窗口 "{window_id}" 当前未连接（可用窗口：'
                f"{available or '无'}）。请先用 eda_list_windows 查看，"
                "再用 eda_select_window 重新指定；代码没有发送到任何窗口。",
                available=available,
            )

        active = self._ensure_active()
        if active and active in live:
            return active

        raise EdaNotConnectedError(
            "尚未连接任何 EasyEDA 窗口。请先在 EasyEDA 专业版中安装并加载 "
            "run-api-gateway.eext 扩展，并确认扩展设置里勾选了「允许外部交互」。",
            available=[],
        )

    async def execute(
        self,
        code: str,
        *,
        window_id: str | None = None,
        timeout: float | None = None,
    ) -> Any:
        """在 EasyEDA 中执行 ``code``，并返回它 ``return`` 的值。"""
        if not isinstance(code, str) or not code.strip():
            raise BridgeError("code 必须是非空字符串")

        target = self._resolve_window(window_id)
        ws = self._eda_clients[target]
        request_id = P.new_request_id()
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = _PendingRequest(
            future=future, window_id=target, socket=ws
        )

        timeout_s = self.request_timeout if timeout is None else float(timeout)
        try:
            if not await self._safe_send(ws, P.execute_message(request_id, code, target)):
                # 一条消息都没发出去：EDA 里什么都没发生，重试是安全的。
                raise EdaNotConnectedError(
                    f'无法把请求发送给 EasyEDA 窗口 "{target}"',
                    available=list(self._live_clients()),
                )
            try:
                return await asyncio.wait_for(asyncio.shield(future), timeout_s)
            except asyncio.TimeoutError as exc:
                # 注意：这里只是"本地不再等待"。请求已经送达 EasyEDA，
                # 它内部仍在继续执行；把 request_id / 超时值带出去，
                # 方便上层告诉调用方"超时 != 失败"。
                raise EdaTimeoutError(
                    f"请求 {request_id} 在 {int(timeout_s * 1000)}ms 后超时未返回结果",
                    request_id=request_id,
                    timeout_s=timeout_s,
                ) from exc
        finally:
            self._pending.pop(request_id, None)
            if not future.done():
                future.cancel()

    # ─── 在途请求的清理 ─────────────────────────────────────────────────

    def _fail_pending_for_window(self, window_id: str, message: str) -> None:
        """让发往某个窗口 ID 的在途请求失败（窗口改名等场景）。"""
        detail = (
            f"{message}。如果是长任务（自动布线/导出）期间断开，"
            "EasyEDA 内部可能仍在执行，结果不会再通过原连接返回；"
            "请改用只读查询确认实际进度，不要直接重提同一任务。"
        )
        for pending in list(self._pending.values()):
            if pending.window_id == window_id and not pending.future.done():
                pending.future.set_exception(
                    EdaNotConnectedError(
                        detail,
                        delivered=True,
                        available=list(self._live_clients()),
                    )
                )

    def _fail_pending_for_socket(
        self, ws: web.WebSocketResponse, message: str
    ) -> None:
        """某条 EDA 连接断开时，只让发往**这条连接**的在途请求失败。

        按 socket 而不是按 windowId 归属，才能在同名窗口被新连接接管时，
        不误伤新连接上的请求。
        """
        detail = (
            f"{message}。如果是长任务（自动布线/导出）期间断开，"
            "EasyEDA 内部可能仍在执行，结果不会再通过原连接返回；"
            "请改用只读查询确认实际进度，不要直接重提同一任务。"
        )
        for pending in list(self._pending.values()):
            if pending.socket is ws and not pending.future.done():
                pending.future.set_exception(
                    EdaNotConnectedError(
                        detail,
                        delivered=True,
                        available=list(self._live_clients()),
                    )
                )

    def _fail_all_pending(self, message: str) -> None:
        """服务关闭时，让所有在途请求失败。"""
        for pending in list(self._pending.values()):
            if not pending.future.done():
                pending.future.set_exception(BridgeError(message))
        self._pending.clear()
