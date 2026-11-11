"""MCP stdio 传输兼容层（测试脚本共用）。

为什么需要它
------------
官方 ``mcp.client.stdio.stdio_client`` 基于 anyio → asyncio 子进程，在 Windows 上
需要创建 **overlapped 命名管道**。受限环境（例如 DSH 的 Windows 沙箱）会直接拒绝：

    File "asyncio\\windows_utils.py", line 63, in pipe
        h2 = _winapi.CreateFile(...)
    PermissionError: [WinError 5] 拒绝访问

普通匿名管道是允许的，所以这里用 ``subprocess.Popen`` + 后台读线程实现同一套
stdin/stdout JSON-RPC。它仍然是"真实子进程 + 真实 stdio + 真实 MCP 服务器"，
只是不经过命名管道。

有权限的环境里 ``detect()`` 会返回 True，两个脚本自动改走官方实现，行为不变。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import queue
import subprocess
import sys
import threading
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

#: 官方 stdio 客户端能否在本环境使用。由 :func:`detect` 设置。
SDK_STDIO_OK = False


async def detect() -> bool:
    """探测能否用 asyncio 创建带管道的子进程；结果写入模块级 ``SDK_STDIO_OK``。"""
    global SDK_STDIO_OK
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            "",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
        )
    except (PermissionError, OSError):
        SDK_STDIO_OK = False
        return False
    await proc.wait()
    SDK_STDIO_OK = True
    return True


class CallReply:
    """``CallToolResult`` 的最小替身（tool_payload 只读这两个字段）。"""

    def __init__(self, payload: dict[str, Any]) -> None:
        self.structuredContent = payload.get("structuredContent")
        self.content = [
            SimpleNamespace(text=item.get("text"), type=item.get("type"))
            for item in payload.get("content") or []
            if isinstance(item, dict)
        ]


class ListReply:
    """``ListToolsResult`` / ``ListResourcesResult`` / ``ListPromptsResult`` 的替身。"""

    def __init__(self, items: list[Any]) -> None:
        self.tools = items
        self.resources = items
        self.prompts = items


class StdioSession:
    """用同步普通管道驱动 ``python -m easyeda_mcp`` 的 stdio JSON-RPC。"""

    def __init__(self, command: str, args: list[str], cwd: str | None) -> None:
        self.command = command
        self.args = list(args)
        self.cwd = cwd
        self._proc: subprocess.Popen[bytes] | None = None
        self._queue: queue.Queue[bytes | None] = queue.Queue()
        self._next_id = 0
        self._lock = asyncio.Lock()
        self._initialized = False

    async def __aenter__(self) -> "StdioSession":
        self._proc = subprocess.Popen(
            [self.command, *self.args],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            cwd=self.cwd,
            bufsize=0,
        )
        threading.Thread(target=self._reader, daemon=True).start()
        await self.initialize()
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        proc = self._proc
        if proc is None:
            return
        with contextlib.suppress(Exception):
            if proc.stdin is not None:
                proc.stdin.close()
        with contextlib.suppress(Exception):
            proc.terminate()
        with contextlib.suppress(Exception):
            proc.wait(timeout=10)

    def _reader(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        for line in iter(self._proc.stdout.readline, b""):
            self._queue.put(line)
        self._queue.put(None)

    async def _read_message(self, timeout: float) -> dict[str, Any]:
        try:
            line = await asyncio.to_thread(self._queue.get, True, timeout)
        except queue.Empty as exc:
            raise RuntimeError("MCP stdio 服务器在超时内没有响应") from exc
        if line is None:
            raise RuntimeError("MCP stdio 服务器已关闭 stdout")
        text = line.decode("utf-8", "replace").strip()
        if not text:
            return {}
        return json.loads(text)

    async def _write(self, payload: dict[str, Any]) -> None:
        assert self._proc is not None and self._proc.stdin is not None
        self._proc.stdin.write(
            (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        )
        self._proc.stdin.flush()

    async def _rpc(
        self, method: str, params: dict[str, Any], timeout: float = 240.0
    ) -> dict[str, Any]:
        async with self._lock:
            self._next_id += 1
            request_id = self._next_id
            await self._write(
                {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
            )
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                message = await self._read_message(max(0.1, deadline - time.monotonic()))
                if not message or message.get("id") != request_id:
                    continue
                if "error" in message:
                    raise RuntimeError(f"{method} 失败：{message['error']}")
                return message.get("result") or {}
            raise RuntimeError(f"{method} 等待超时")

    async def initialize(self) -> None:
        if self._initialized:
            return
        await self._rpc(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "easyeda-mcp-tests", "version": "1"},
            },
            timeout=60.0,
        )
        await self._write({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self._initialized = True

    async def list_tools(self) -> ListReply:
        result = await self._rpc("tools/list", {})
        return ListReply(
            [SimpleNamespace(name=item.get("name")) for item in result.get("tools") or []]
        )

    async def list_resources(self) -> ListReply:
        result = await self._rpc("resources/list", {})
        return ListReply(
            [SimpleNamespace(uri=item.get("uri")) for item in result.get("resources") or []]
        )

    async def list_prompts(self) -> ListReply:
        result = await self._rpc("prompts/list", {})
        return ListReply(
            [SimpleNamespace(name=item.get("name")) for item in result.get("prompts") or []]
        )

    async def call_tool(
        self, name: str, arguments: dict[str, Any] | None = None
    ) -> CallReply:
        result = await self._rpc("tools/call", {"name": name, "arguments": arguments or {}})
        return CallReply(result)


@asynccontextmanager
async def open_stdio(params: Any):
    """产出 ``(read_stream, write_stream)``：官方 SDK 可用时优先用它。"""
    if SDK_STDIO_OK:
        from mcp import stdio_client

        async with stdio_client(params) as streams:
            yield streams
        return

    session = StdioSession(params.command, list(params.args), params.cwd)
    await session.__aenter__()
    try:
        yield (session, None)
    finally:
        await session.__aexit__(None, None, None)


@asynccontextmanager
async def open_session(read_stream: Any, write_stream: Any):
    """产出 ClientSession；fallback 时直接交还 ``StdioSession``。"""
    if SDK_STDIO_OK:
        from mcp import ClientSession

        async with ClientSession(read_stream, write_stream) as session:
            yield session
        return
    yield read_stream
