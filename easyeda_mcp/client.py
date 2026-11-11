"""MCP 工具用来访问桥接服务的 HTTP 客户端。

即使桥接服务就运行在同一个进程里，MCP -> 桥接的调用也统一走 HTTP，
这样 `curl`、外部脚本和 MCP 工具走的是同一条代码路径，并且第二个 MCP
进程可以直接复用第一个进程启动的桥接服务。
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any

import aiohttp

from . import protocol as P
from .errors import BridgeError, EdaNotConnectedError, EdaTimeoutError

logger = logging.getLogger(__name__)

#: 扫描 49620-49629 时每个端口的探测预算（Node.js 版为 800ms）。
DISCOVERY_TIMEOUT = 0.35

#: HTTP 调用在执行超时之外额外留出的余量。
_HTTP_MARGIN = 5.0


@dataclass(slots=True)
class ExecutionOutcome:
    """一次 ``POST /execute`` 往返的结果。"""

    result: Any
    window_id: str | None


class BridgeClient:
    """访问 ``http://host:port`` 上运行的 EasyEDA 桥接服务。"""

    def __init__(
        self,
        *,
        host: str = P.DEFAULT_HOST,
        port: int | None = None,
        request_timeout: float = P.DEFAULT_REQUEST_TIMEOUT,
    ) -> None:
        self.host = host
        self.port = port
        self.request_timeout = request_timeout

    def __repr__(self) -> str:  # pragma: no cover - 仅用于调试输出
        return f"BridgeClient(host={self.host!r}, port={self.port!r})"

    @property
    def base_url(self) -> str:
        """桥接服务的基础地址。"""
        return f"http://{self.host}:{self.port}"

    @property
    def known(self) -> bool:
        """是否已经知道桥接服务端口。"""
        return self.port is not None

    # ─── 端口发现 ───────────────────────────────────────────────────────

    @classmethod
    async def discover(
        cls,
        *,
        host: str = P.DEFAULT_HOST,
        port_start: int = P.PORT_START,
        port_end: int = P.PORT_END,
        timeout: float = DISCOVERY_TIMEOUT,
    ) -> "BridgeClient | None":
        """扫描端口范围，返回第一个可用桥接服务对应的客户端。"""
        for port in range(port_start, port_end + 1):
            candidate = cls(host=host, port=port)
            try:
                payload = await candidate._request_json("GET", "/health", timeout=timeout)
            except (BridgeError, aiohttp.ClientError, asyncio.TimeoutError, OSError):
                continue
            if P.is_bridge_health(payload):
                logger.debug("在端口 %s 上发现已有桥接服务", port)
                return candidate
        return None

    # ─── 传输层 ─────────────────────────────────────────────────────────

    async def _request_json(
        self, method: str, path: str, *, json_body: Any = None, timeout: float | None = None
    ) -> Any:
        """发送请求并返回解析后的 JSON；HTTP 4xx/5xx 会转成对应异常。"""
        status, payload = await self._request(
            method, path, json_body=json_body, timeout=timeout
        )
        if status >= 400:
            raise self._error_from_payload(status, payload, path)
        return payload

    def _error_from_payload(self, status: int, payload: Any, path: str) -> BridgeError:
        """把桥接服务的错误响应还原成**带完整字段**的异常。

        关键点：桥接返回的 ``timeout`` / ``requestId`` / ``delivered`` 这些字段
        必须在 MCP 层仍然可见。早期实现只取 ``error`` 文本，于是"超时但仍在
        执行"这个最关键的信息在 MCP 工具返回里丢了一半；更糟的是靠文本里是否
        出现 "超时" 来判断类型，EDA 自己抛出的、恰好含 "timed out" 的业务错误
        会被误判成超时，进而被当作"不是失败"而放弃处理。
        """
        fields: dict[str, Any] = payload if isinstance(payload, dict) else {}
        message = fields.get("error")
        if not isinstance(message, str) or not message:
            message = f"桥接服务对 {path} 返回了 HTTP {status}"

        available = fields.get("availableWindows")
        available = [str(item) for item in available] if isinstance(available, list) else []

        if status == 503:
            return EdaNotConnectedError(
                message,
                delivered=fields.get("delivered") is True,
                available=available,
                details=fields,
            )
        # 只认服务端显式标记，文本匹配仅用于兼容旧版桥接（没有该字段）
        if fields.get("timeout") is True or (
            "timeout" not in fields
            and ("超时" in message or "timed out" in message.lower())
        ):
            timeout_s = fields.get("timeoutSeconds")
            request_id = fields.get("requestId")
            return EdaTimeoutError(
                message,
                request_id=request_id if isinstance(request_id, str) else None,
                timeout_s=float(timeout_s) if isinstance(timeout_s, (int, float)) else None,
                still_running=fields.get("stillRunning") is not False,
                details=fields,
            )
        return BridgeError(message, details=fields)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        timeout: float | None = None,
    ) -> tuple[int, Any]:
        """真正发起 HTTP 请求，返回 (状态码, JSON 载荷)。"""
        if self.port is None:
            raise EdaNotConnectedError(
                "EasyEDA 桥接服务未运行。请用 `python -m easyeda_mcp` 启动它"
                f"（监听端口 {P.PORT_START}-{P.PORT_END}）。"
            )

        total = self.request_timeout if timeout is None else float(timeout)
        url = f"{self.base_url}{path}"
        client_timeout = aiohttp.ClientTimeout(total=total)

        try:
            async with aiohttp.ClientSession(timeout=client_timeout) as session:
                async with session.request(
                    method, url, json=json_body, headers={"Accept": "application/json"}
                ) as response:
                    status = response.status
                    text = await response.text()
        except (asyncio.TimeoutError, aiohttp.ServerTimeoutError) as exc:
            raise EdaTimeoutError(
                f"{method} {path} 在 {total:.1f}s 后超时（桥接服务 {self.base_url}）"
            ) from exc
        except (aiohttp.ClientError, OSError) as exc:
            raise EdaNotConnectedError(
                f"无法连接 {self.base_url} 上的 EasyEDA 桥接服务：{exc}。"
                "桥接进程是否已经退出？"
            ) from exc

        if status == 204 or not text:
            return status, None
        try:
            return status, json.loads(text)
        except json.JSONDecodeError as exc:
            raise BridgeError(
                f"桥接服务对 {path} 返回了非 JSON 内容：{text[:200]!r}"
            ) from exc

    # ─── 对外接口 ───────────────────────────────────────────────────────

    async def health(self) -> dict[str, Any]:
        """原始 ``/health`` 载荷（服务身份 + 连接状态）。"""
        payload = await self._request_json("GET", "/health")
        if not isinstance(payload, dict):
            raise BridgeError("/health 返回了非预期的载荷")
        return payload

    async def list_windows(self) -> dict[str, Any]:
        """原始 ``/eda-windows`` 载荷。"""
        payload = await self._request_json("GET", "/eda-windows")
        if not isinstance(payload, dict):
            raise BridgeError("/eda-windows 返回了非预期的载荷")
        return payload

    async def select_window(self, window_id: str) -> dict[str, Any]:
        """把 ``window_id`` 设为当前活动的 EasyEDA 窗口。"""
        payload = await self._request_json(
            "POST", "/eda-windows/select", json_body={"windowId": window_id}
        )
        if not isinstance(payload, dict):
            raise BridgeError("/eda-windows/select 返回了非预期的载荷")
        return payload

    async def ping(self, *, window_id: str | None = None) -> ExecutionOutcome:
        """执行一段极简代码，用来确认窗口真的在线并测量往返延迟。"""
        return await self.execute(
            "return { pong: true, at: Date.now() };", window_id=window_id, timeout=10.0
        )

    async def execute(
        self,
        code: str,
        *,
        window_id: str | None = None,
        timeout: float | None = None,
    ) -> ExecutionOutcome:
        """在 EasyEDA 中执行 JavaScript，并返回它 ``return`` 的值。"""
        body: dict[str, Any] = {"code": code}
        if window_id:
            body["windowId"] = window_id

        effective_timeout = self.request_timeout if timeout is None else float(timeout)
        # 把超时也告诉服务端，让两边的等待时间一致；否则服务端仍按
        # DEFAULT_REQUEST_TIMEOUT（30s）提前放弃长任务，客户端却还在等。
        body["timeout"] = effective_timeout
        payload = await self._request_json(
            "POST", "/execute", json_body=body, timeout=effective_timeout + _HTTP_MARGIN
        )
        if not isinstance(payload, dict) or not payload.get("success"):
            raise self._error_from_payload(500, payload, "/execute")
        return ExecutionOutcome(
            result=payload.get("result"), window_id=payload.get("windowId")
        )
