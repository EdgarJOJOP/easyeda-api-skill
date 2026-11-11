"""真实 EasyEDA 联调验证（只读，不会修改你的工程）。

与 ``verify_all_branches.py`` 的区别：那一套用 FakeEda 复刻扩展契约，能穷举分支；
这一套连**真实运行中的 EasyEDA 专业版**，验证"整条链路真的能驱动整个 EDA API"：

    MCP 客户端 → easyeda_mcp(stdio) → 桥接(HTTP+WS) → run-api-gateway 扩展 → eda.*

默认全部为只读操作（取环境信息、查工程/文档、数图元、跑算术、读写字符串），
不会创建、修改或删除任何图元。要验证写操作，请显式加 ``--allow-mutation``，
并且只在**你自己的测试工程**上运行。

用法::

    D:/env/mcp/python.exe D:/eda/scripts/verify_live_eda.py
    D:/env/mcp/python.exe D:/eda/scripts/verify_live_eda.py --wait 120 --verbose
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def tool_payload(result: Any) -> dict[str, Any]:
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


class Checks:
    def __init__(self, verbose: bool = False) -> None:
        self.verbose = verbose
        self.passed = 0
        self.failed = 0
        self.rows: list[tuple[bool, str, str]] = []

    def check(self, name: str, ok: Any, detail: Any = "") -> bool:
        ok = bool(ok)
        text = "" if ok else _short(detail)
        self.rows.append((ok, name, text))
        if ok:
            self.passed += 1
            if self.verbose:
                print(f"  [PASS] {name}")
        else:
            self.failed += 1
            print(f"  [FAIL] {name}" + (f"  -> {text}" if text else ""))
        return ok

    def equal(self, name: str, actual: Any, expected: Any) -> bool:
        return self.check(name, actual == expected, f"actual={_short(actual)} expected={_short(expected)}")


def _short(value: Any, limit: int = 300) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, default=repr)
    except (TypeError, ValueError):
        text = repr(value)
    return text if len(text) <= limit else text[:limit] + "…"


async def call(session: Any, tool: str, **arguments: Any) -> dict[str, Any]:
    """调用一个 MCP 工具（参数名用 tool，避免与工具自己的 name 参数撞名）。"""
    return tool_payload(await session.call_tool(tool, arguments))


async def wait_for_eda(session: Any, wait: float) -> dict[str, Any]:
    """等待至少一个真实 EasyEDA 窗口连上桥接。"""
    deadline = time.monotonic() + wait
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        last = await call(session, "eda_status")
        eda = last.get("eda", {})
        if eda.get("connected"):
            return last
        await asyncio.sleep(1.0)
    return last


async def run(wait: float, verbose: bool, allow_mutation: bool) -> int:
    from mcp import ClientSession, StdioServerParameters, stdio_client

    checks = Checks(verbose=verbose)
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "easyeda_mcp", "--log-level", "WARNING"],
        cwd=str(ROOT),
    )

    async with stdio_client(params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            print(f"等待 EasyEDA 扩展连接（最多 {int(wait)} 秒）……")
            status = await wait_for_eda(session, wait)
            eda_info = status.get("eda", {})
            if not eda_info.get("connected"):
                print("\n没有检测到已连接的 EasyEDA 窗口。请检查：")
                print("  1. 嘉立创EDA 专业版桌面客户端已打开（浏览器版无扩展宿主环境）")
                print("  2. 扩展管理器中 Run API Gateway 已启用")
                print("  3. 该扩展的「允许外部交互」权限已勾选")
                print("  4. 扩展菜单 API Gateway → About... 显示 Connected")
                print("\n桥接状态：")
                print(json.dumps(status, ensure_ascii=False, indent=2)[:1200])
                return 2

            window_id = eda_info.get("activeWindowId")
            print(f"已连接：windowId={window_id}，窗口数={eda_info.get('windowCount')}\n")

            print("=== L1 连接与窗口 ===")
            checks.equal("L1 桥接在运行", status.get("bridge", {}).get("running"), True)
            checks.equal("L1 至少一个窗口", eda_info.get("windowCount", 0) >= 1, True)
            checks.check("L1 记录了 windowId", isinstance(window_id, str) and bool(window_id), window_id)

            print("\n=== L2 最基本的代码执行 ===")
            basic = await call(session, "eda_execute", code="return 1 + 1;")
            checks.equal("L2 执行成功", basic.get("ok"), True)
            checks.equal("L2 返回 2", basic.get("result"), 2)

            print("\n=== L3 真实扩展运行环境 ===")
            env = await call(
                session,
                "eda_execute",
                code=(
                    "return {"
                    " isClient: eda.sys_Environment.isClient(),"
                    " isHalfOffline: eda.sys_Environment.isHalfOfflineMode(),"
                    " version: eda.sys_Environment.getEditorCurrentVersion(),"
                    " hasEda: typeof eda === 'object'"
                    "};"
                ),
            )
            checks.equal("L3 取到环境信息", env.get("ok"), True)
            env_result = env.get("result") or {}
            checks.check("L3 eda 对象存在", env_result.get("hasEda") is True, env_result)
            checks.check(
                "L3 是桌面客户端（扩展宿主环境）",
                env_result.get("isClient") is True,
                env_result,
            )
            checks.check("L3 能读到编辑器版本", bool(env_result.get("version")), env_result)

            print("\n=== L4 全量 API 可达性 ===")
            surface = await call(
                session,
                "eda_execute",
                code=(
                    "const keys = Object.keys(eda);"
                    "const need = ['dmt_Project','dmt_Board','dmt_Schematic','dmt_EditorControl',"
                    "'pcb_Primitive','pcb_PrimitiveLine','pcb_PrimitiveVia','pcb_PrimitivePour',"
                    "'pcb_Document','pcb_Drc','sch_Primitive','sch_PrimitiveComponent',"
                    "'sch_PrimitiveWire','sch_Document','sch_Drc','lib_Device','lib_Symbol',"
                    "'lib_Footprint','sys_FileSystem','sys_Dialog','sys_WebSocket','sys_Message'];"
                    "return {count: keys.length, keys: keys,"
                    " missing: need.filter(k => !(k in eda))};"
                ),
            )
            checks.equal("L4 能枚举 eda 命名空间", surface.get("ok"), True)
            surface_result = surface.get("result") or {}
            checks.check(
                "L4 API 模块数量 > 50",
                isinstance(surface_result.get("count"), int) and surface_result["count"] > 50,
                surface_result.get("count"),
            )
            checks.equal("L4 关键模块都存在", surface_result.get("missing"), [])

            print("\n=== L5 枚举（网关作用域没有枚举符号，靠 MCP 注入） ===")
            # 注意：探测必须关掉注入，否则连 "typeof EPCB_LayerId" 都会被注入成 object
            raw_enum = await call(
                session,
                "eda_execute",
                code="return typeof EPCB_LayerId;",
                inject_enums=False,
            )
            checks.equal(
                "L5 实测：网关作用域里没有枚举符号", raw_enum.get("result"), "undefined"
            )
            enum_info = await call(session, "eda_enum", name="EPCB_LayerId")
            checks.equal("L5 eda_enum 可查枚举", enum_info.get("ok"), True)
            checks.equal(
                "L5 EPCB_LayerId.TOP 取值正确",
                (enum_info.get("members") or {}).get("TOP"),
                1,
            )
            injected = await call(session, "eda_execute", code="return EPCB_LayerId.TOP;")
            checks.equal("L5 注入后枚举可用", injected.get("result"), 1)
            checks.equal("L5 上报注入项", injected.get("injectedEnums"), ["EPCB_LayerId"])
            refused = await call(
                session, "eda_execute", code="return EPCB_LayerId.TOP;", inject_enums=False
            )
            checks.equal("L5 关掉注入时如实失败", refused.get("ok"), False)
            checks.check(
                "L5 失败原因是 ReferenceError",
                "EPCB_LayerId" in str(refused.get("error")),
                refused.get("error"),
            )

            print("\n=== L6 工程与文档状态（只读） ===")
            state = await call(
                session,
                "eda_execute",
                code=(
                    "const project = await eda.dmt_Project.getCurrentProjectInfo();"
                    "const doc = await eda.dmt_SelectControl.getCurrentDocumentInfo();"
                    "return {project: project ? {name: project.friendlyName || project.name,"
                    " uuid: project.uuid} : null, doc: doc || null};"
                ),
            )
            checks.equal("L6 能读工程/文档状态", state.get("ok"), True)
            checks.check("L6 返回结构完整", isinstance(state.get("result"), dict), state.get("result"))
            state_result = state.get("result") or {}
            print(f"  当前工程：{_short(state_result.get('project'))}")
            print(f"  当前文档：{_short(state_result.get('doc'))}")

            print("\n=== L7 结果保真（中文/嵌套/null/大字符串） ===")
            fidelity = await call(
                session,
                "eda_execute",
                code=(
                    "return {text: '嘉立创EDA 专业版 ✓ 🎉', nested: {a: [1, 2, null], b: {c: true}},"
                    " nil: null, float: 1.25, negative: -3};"
                ),
            )
            expected = {
                "text": "嘉立创EDA 专业版 ✓ 🎉",
                "nested": {"a": [1, 2, None], "b": {"c": True}},
                "nil": None,
                "float": 1.25,
                "negative": -3,
            }
            checks.equal("L7 结果逐字段一致", fidelity.get("result"), expected)

            big = await call(
                session,
                "eda_execute",
                code="return 'y'.repeat(1024 * 1024);",
                timeout_seconds=60,
            )
            checks.equal("L7 1MB 字符串完整回传", len(big.get("result") or ""), 1024 * 1024)

            print("\n=== L8 错误透传 ===")
            failure = await call(session, "eda_execute", code="throw new Error('live-probe-error');")
            checks.equal("L8 抛错时 ok=false", failure.get("ok"), False)
            checks.check(
                "L8 错误文本来自 EDA",
                "live-probe-error" in str(failure.get("error")),
                failure.get("error"),
            )

            print("\n=== L9 超时语义（超时 ≠ 失败） ===")
            started = time.perf_counter()
            timeout = await call(
                session,
                "eda_execute",
                code="await new Promise(r => setTimeout(r, 2500)); return 'done';",
                timeout_seconds=0.8,
            )
            elapsed = time.perf_counter() - started
            checks.equal("L9 超时 ok=false", timeout.get("ok"), False)
            checks.equal("L9 timeout=true", timeout.get("timeout"), True)
            checks.equal("L9 stillRunning=true", timeout.get("stillRunning"), True)
            checks.check("L9 带 requestId", bool(timeout.get("requestId")), timeout.get("requestId"))
            checks.check("L9 0.8s 左右返回", 0.5 <= elapsed <= 4.0, round(elapsed, 2))

            print("\n=== L10 并发（4 路只读） ===")
            codes = [f"return {index} * 10;" for index in range(4)]
            outcomes = await asyncio.gather(
                *(call(session, "eda_execute", code=code) for code in codes)
            )
            checks.equal(
                "L10 并发结果一一对应",
                [outcome.get("result") for outcome in outcomes],
                [0, 10, 20, 30],
            )

            print("\n=== L11 心跳稳定性（等 18 秒，跨过一次 15s 心跳） ===")
            await asyncio.sleep(18)
            after = await call(session, "eda_status")
            checks.equal("L11 心跳后仍连接", after.get("eda", {}).get("connected"), True)
            alive = await call(session, "eda_execute", code="return 'still-alive';")
            checks.equal("L11 心跳后仍可执行", alive.get("result"), "still-alive")

            if allow_mutation:
                print("\n=== L12 写操作（显式开启） ===")
                created = await call(
                    session,
                    "eda_execute",
                    code=(
                        "const tab = await eda.dmt_EditorControl.openDocument("
                        "  (await eda.dmt_SelectControl.getCurrentDocumentInfo() || {}).uuid);"
                        "return {tabId: tab || null};"
                    ),
                )
                checks.check("L12 文档可用（未做破坏性写入）", created.get("ok") is not None, created)

    print("\n" + "=" * 70)
    print(f"真实 EDA 联调：通过 {checks.passed} / {checks.passed + checks.failed}，失败 {checks.failed}")
    if checks.failed:
        for ok, name, detail in checks.rows:
            if not ok:
                print(f"  - {name} -> {detail}")
    print("=" * 70)
    return 0 if checks.failed == 0 else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="真实 EasyEDA 只读联调验证")
    parser.add_argument("--wait", type=float, default=60, help="等待 EDA 连接的秒数")
    parser.add_argument("--verbose", action="store_true", help="打印每一条通过项")
    parser.add_argument(
        "--allow-mutation",
        action="store_true",
        help="允许写操作（默认关闭；只在你自己的测试工程上使用）",
    )
    args = parser.parse_args()
    print(f"解释器：{sys.executable}\n项目根目录：{ROOT}")
    try:
        return asyncio.run(run(args.wait, args.verbose, args.allow_mutation))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
