"""EasyEDA MCP 服务的端到端自测脚本。

它会用 stdio 方式启动真实的 MCP 服务，在桥接 WebSocket 端点上模拟一个
EasyEDA 专业版客户端，然后通过 MCP 协议把每个工具都调用一遍。

用法::

    python scripts/selftest.py            # 自动挑一个空闲端口
    python scripts/selftest.py --port 49650 --verbose
"""

import argparse
import asyncio
import json
import socket
import sys
from pathlib import Path
from typing import Any

import aiohttp
from mcp import StdioServerParameters

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from easyeda_mcp import protocol as P  # noqa: E402
from easyeda_mcp.client import BridgeClient  # noqa: E402
from mcp_stdio_compat import (  # noqa: E402
    detect as detect_stdio,
    open_session,
    open_stdio,
)

ROOT = Path(__file__).resolve().parent.parent
WINDOW_ID = "fake-window-1"
FAIL_MARKER = "模拟 EDA 返回的错误"


class Checks:
    """极简断言收集器：单项失败不会中断后续检查。"""

    def __init__(self) -> None:
        self.passed = 0
        self.failures: list[str] = []

    def check(self, name: str, condition: bool, detail: Any = "") -> bool:
        if condition:
            self.passed += 1
            print(f"  通过  {name}")
        else:
            self.failures.append(name if not detail else f"{name}（{detail}）")
            print(f"  失败  {name}" + (f" -> {detail}" if detail else ""))
        return bool(condition)

    def report(self) -> int:
        total = self.passed + len(self.failures)
        print()
        if self.failures:
            print(f"失败：{len(self.failures)}/{total} 项未通过")
            for failure in self.failures:
                print(f"  - {failure}")
            return 1
        print(f"全部通过：{self.passed}/{total}")
        return 0


def pick_free_port() -> int:
    """向系统申请一个当前空闲的端口。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def tool_payload(result: Any) -> dict[str, Any]:
    """读取 CallToolResult 中的结构化载荷（兼容不同 mcp 版本）。"""
    data = getattr(result, "structuredContent", None)
    if data is None:
        data = getattr(result, "structured_content", None)
    if isinstance(data, dict):
        # 返回类型不是 object 时，SDK 会包一层 {"result": ...}
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


class FakeEda:
    """最小化的 EasyEDA 专业版替身：注册窗口并响应执行请求。"""

    def __init__(self, port: int, window_id: str = WINDOW_ID) -> None:
        self.port = port
        self.window_id = window_id
        self.ready = asyncio.Event()
        self.handshake: dict[str, Any] | None = None
        self.codes: list[str] = []

    async def run(self) -> None:
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(f"ws://127.0.0.1:{self.port}/eda") as ws:
                self.handshake = await ws.receive_json()
                await ws.send_json({"type": P.MSG_REGISTER, "windowId": self.window_id})
                self.ready.set()
                async for message in ws:
                    if message.type is not aiohttp.WSMsgType.TEXT:
                        continue
                    payload = json.loads(message.data)
                    kind = payload.get("type")
                    if kind == P.MSG_PING:
                        await ws.send_json({"type": P.MSG_PONG, "id": payload.get("id")})
                        continue
                    if kind != P.MSG_EXECUTE:
                        continue
                    code = payload.get("code", "")
                    self.codes.append(code)
                    if "__timeout__" in code:
                        continue  # 故意不回复，用来触发超时分支
                    if "__fail__" in code:
                        await ws.send_json(
                            {
                                "type": P.MSG_ERROR,
                                "id": payload.get("id"),
                                "error": FAIL_MARKER,
                            }
                        )
                        continue
                    await ws.send_json(
                        {
                            "type": P.MSG_RESULT,
                            "id": payload.get("id"),
                            "result": {"echo": code, "window": self.window_id},
                        }
                    )


async def wait_for_bridge(port: int, timeout: float = 20.0) -> bool:
    """轮询等待桥接服务启动完成。"""
    client = BridgeClient(port=port)
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        try:
            payload = await client.health()
        except Exception:  # noqa: BLE001 - 桥接服务可能还没起来
            await asyncio.sleep(0.2)
            continue
        if P.is_bridge_health(payload):
            return True
        await asyncio.sleep(0.2)
    return False


async def call(session: Any, name: str, **arguments: Any) -> dict[str, Any]:
    """调用一个工具并返回其结构化载荷。"""
    return tool_payload(await session.call_tool(name, arguments))


async def run(port: int, verbose: bool) -> int:
    """执行全部自测用例。"""
    checks = Checks()
    # 官方 stdio 客户端依赖命名管道，受限环境会失败；公共模块会在有权限时自动切回官方实现
    await detect_stdio()
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "easyeda_mcp", "--port", str(port), "--log-level", "WARNING"],
        cwd=str(ROOT),
    )

    async with open_stdio(params) as (read_stream, write_stream):
        async with open_session(read_stream, write_stream) as session:
            await session.initialize()

            print("\n[1] MCP 接口面")
            tools = await session.list_tools()
            tool_names = {tool.name for tool in tools.tools}
            expected = {
                "eda_status",
                "eda_list_windows",
                "eda_select_window",
                "eda_execute",
                "eda_execute_batch",
                "eda_ping",
                "eda_api_search",
                "eda_api_doc",
            }
            checks.check("全部工具已注册", expected <= tool_names, sorted(tool_names))
            resources = await session.list_resources()
            uris = {str(resource.uri) for resource in resources.resources}
            checks.check(
                "文档资源已注册",
                {"easyeda://skill", "easyeda://references/index"} <= uris,
                sorted(uris),
            )
            if verbose:
                for tool in tools.tools:
                    print(f"        - {tool.name}: {tool.description.splitlines()[0]}")

            print("\n[2] 桥接可达，但尚未连接 EasyEDA")
            checks.check("桥接服务可达", await wait_for_bridge(port), f"端口 {port}")
            status = await call(session, "eda_status")
            checks.check("状态显示桥接在运行", status.get("bridge", {}).get("running") is True, status)
            checks.check(
                "状态显示没有 EDA 窗口",
                status.get("eda", {}).get("connected") is False,
                status.get("eda"),
            )
            no_eda = await call(session, "eda_execute", code="return 1;")
            checks.check(
                "未连接时执行会失败",
                no_eda.get("ok") is False and "尚未连接" in str(no_eda.get("error")),
                no_eda,
            )

            print("\n[3] EasyEDA 客户端接入")
            fake = FakeEda(port)
            fake_task = asyncio.create_task(fake.run())
            await asyncio.wait_for(fake.ready.wait(), 10)
            checks.check(
                "握手报文带有服务标识",
                (fake.handshake or {}).get("service") == P.SERVICE_ID,
                fake.handshake,
            )
            checks.check(
                "握手报文标明 EDA 客户端类型",
                (fake.handshake or {}).get("clientType") == "eda",
                fake.handshake,
            )
            status = await call(session, "eda_status")
            checks.check(
                "状态能看到 EDA 窗口",
                status.get("eda", {}).get("windowCount") == 1
                and status.get("eda", {}).get("activeWindowId") == WINDOW_ID,
                status.get("eda"),
            )

            print("\n[4] 代码执行")
            outcome = await call(session, "eda_execute", code="return 1 + 1;")
            checks.check("执行返回 EDA 的结果", outcome.get("ok") is True, outcome)
            checks.check(
                "执行结果经由桥接回传",
                outcome.get("result", {}).get("echo") == "return 1 + 1;",
                outcome.get("result"),
            )
            checks.check("执行结果标明窗口", outcome.get("windowId") == WINDOW_ID, outcome)

            failure = await call(session, "eda_execute", code="__fail__")
            checks.check(
                "EDA 返回的错误会上报",
                failure.get("ok") is False and FAIL_MARKER in str(failure.get("error")),
                failure,
            )

            timeout = await call(
                session, "eda_execute", code="__timeout__", timeout_seconds=1.0
            )
            checks.check(
                "超时会被上报",
                timeout.get("ok") is False and "超时" in str(timeout.get("error")),
                timeout,
            )

            batch = await call(
                session,
                "eda_execute_batch",
                steps=[{"name": "第一步", "code": "return 'a';"}, {"code": "return 'b';"}],
            )
            checks.check(
                "批量执行跑完所有步骤",
                batch.get("ok") is True and batch.get("executed") == 2,
                batch,
            )

            ping = await call(session, "eda_ping")
            checks.check("ping 能往返", ping.get("ok") is True, ping)

            print("\n[5] 窗口管理")
            windows = await call(session, "eda_list_windows")
            checks.check(
                "列出窗口",
                windows.get("count") == 1 and windows.get("activeWindowId") == WINDOW_ID,
                windows,
            )
            selected = await call(session, "eda_select_window", window_id=WINDOW_ID)
            checks.check("选择窗口", selected.get("ok") is True, selected)
            missing = await call(session, "eda_select_window", window_id="不存在的窗口")
            checks.check("未知窗口会被拒绝", missing.get("ok") is False, missing)

            print("\n[6] 文档工具")
            search = await call(session, "eda_api_search", query="getCurrentProjectInfo")
            checks.check(
                "API 检索有命中",
                search.get("ok") is True and bool(search.get("matches")),
                search.get("matches", [])[:1],
            )
            doc = await call(session, "eda_api_doc", path="references/classes/DMT_Project.md")
            checks.check(
                "API 文档返回带行号内容",
                doc.get("ok") is True and "getCurrentProjectInfo" in str(doc.get("text")),
                doc.get("path"),
            )
            escape = await call(session, "eda_api_doc", path="../../../Windows/win.ini")
            checks.check("API 文档阻止路径穿越", escape.get("ok") is False, escape)

            print("\n[7] EDA 断开连接")
            fake_task.cancel()
            try:
                await fake_task
            except (asyncio.CancelledError, aiohttp.ClientError):
                pass
            await asyncio.sleep(0.5)
            status = await call(session, "eda_status")
            checks.check(
                "状态能发现连接已断开",
                status.get("eda", {}).get("connected") is False,
                status.get("eda"),
            )

    return checks.report()


def main() -> int:
    """脚本入口。"""
    parser = argparse.ArgumentParser(description="EasyEDA MCP 端到端自测")
    parser.add_argument("--port", type=int, default=None, help="桥接服务使用的端口")
    parser.add_argument("--verbose", action="store_true", help="打印工具明细")
    args = parser.parse_args()
    port = args.port or pick_free_port()
    print(f"EasyEDA MCP 端到端自测（桥接端口 {port}）")
    try:
        return asyncio.run(run(port, args.verbose))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
