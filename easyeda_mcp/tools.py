"""暴露给 AI 智能体的 MCP 工具。

每个工具都是桥接 HTTP 接口 + 文档索引的一层薄封装，参数与返回值都使用
易于机器处理的结构化类型。工具名（如 ``eda_execute``）是稳定标识符，
随项目分发的 SKILL.md 中按名称引用它们，因此不要随意更名。
"""

import asyncio
import json
import logging
import re
import time
from typing import Any

from . import protocol as P
from .client import BridgeClient, ExecutionOutcome
from .docs import DocError, DocIndex, json_safe
from .enums import EnumIndex
from .drc_guard import (
    ACTIONS as DRC_ACTIONS,
    build_drc_js,
    normalize_drc_result,
)
from . import circuit_sim
from . import netlist_builder
from . import layout_advisor
from .errors import BridgeError, EdaNotConnectedError, EdaTimeoutError
from .mcp_compat import MCP_SDK, MCPServer

logger = logging.getLogger(__name__)

_HINT_NOT_CONNECTED = (
    "当前没有已连接的 EasyEDA 专业版窗口。请在 EasyEDA 专业版中安装并加载 "
    "`run-api-gateway.eext` 扩展"
    "（https://jlc-ext.com/item/oshwhub/run-api-gateway），"
    f'并确认它已通过服务标识 "{P.SERVICE_ID}" 的握手校验。'
)

#: 一次性导出整板几何（焊盘/走线/过孔/器件/板框），供通道分析使用。
_GEOMETRY_JS = r"""
function dimsOf(state) {
  if (state === null || state === undefined) return { shape: '?', w: 0, h: 0 };
  let a = state;
  if (typeof a === 'string') { try { a = JSON.parse(a); } catch (e) { return { shape: '?', w: 0, h: 0 }; } }
  if (!Array.isArray(a)) return { shape: '?', w: 0, h: 0 };
  return { shape: String(a[0] || ''), w: Number(a[1]) || 0, h: Number(a[2]) || 0 };
}
function holeOf(state) {
  if (state === null || state === undefined) return null;
  let a = state;
  if (typeof a === 'string') { try { a = JSON.parse(a); } catch (e) { return null; } }
  if (!Array.isArray(a)) return null;
  return { shape: String(a[0] || ''), d: Number(a[1]) || 0 };
}
let outline = null;
for (const id of (await eda.pcb_PrimitivePolyline.getAllPrimitiveId()) || []) {
  const got = await eda.pcb_PrimitivePolyline.get([id]);
  const one = Array.isArray(got) ? got[0] : got;
  if (one && one.getState_Layer() === 11) {
    const poly = one.getState_Polygon();
    const raw = poly && poly.polygon ? poly.polygon : [];
    outline = raw.filter((v) => typeof v === 'number');
  }
}
const compList = (await eda.pcb_PrimitiveComponent.getAll()) || [];
const comps = [];
const owner = new Map();
for (const c of compList) {
  const cid = c.getState_PrimitiveId();
  const des = c.getState_Designator() || '';
  comps.push({ des, x: c.getState_X(), y: c.getState_Y(), rot: c.getState_Rotation(), layer: c.getState_Layer() });
  let list = [];
  try { const raw = c.getState_Pads(); list = Array.isArray(raw) ? raw : JSON.parse(raw || '[]'); } catch (e) { }
  for (const it of list) owner.set(cid + String(it.primitiveId), des);
}
const pads = [];
for (const p of (await eda.pcb_PrimitivePad.getAll()) || []) {
  const d = dimsOf(p.getState_Pad());
  const h = holeOf(p.getState_Hole());
  let w = d.w, hh = d.h;
  if (h && (!w || !hh)) { if (!w) w = h.d; if (!hh) hh = h.d; }
  const pid = p.getState_PrimitiveId();
  pads.push({
    des: owner.get(pid) || '', pad: p.getState_PadNumber(), net: p.getState_Net() || '',
    x: p.getState_X(), y: p.getState_Y(), rot: p.getState_Rotation(),
    shape: d.shape, w, h: hh, layer: p.getState_Layer(), pid,
  });
}
const lines = [];
for (const id of (await eda.pcb_PrimitiveLine.getAllPrimitiveId()) || []) {
  const got = await eda.pcb_PrimitiveLine.get([id]);
  const one = Array.isArray(got) ? got[0] : got;
  if (!one) continue;
  lines.push({ id, net: one.getState_Net() || '', layer: one.getState_Layer(),
    x1: one.getState_StartX(), y1: one.getState_StartY(), x2: one.getState_EndX(), y2: one.getState_EndY(),
    width: one.getState_LineWidth() });
}
const vias = [];
for (const id of (await eda.pcb_PrimitiveVia.getAllPrimitiveId()) || []) {
  const got = await eda.pcb_PrimitiveVia.get([id]);
  const one = Array.isArray(got) ? got[0] : got;
  if (!one) continue;
  vias.push({ id, net: one.getState_Net() || '', x: one.getState_X(), y: one.getState_Y(),
    dia: one.getState_Diameter ? one.getState_Diameter() : 24 });
}
const pours = [];
for (const id of (await eda.pcb_PrimitivePour.getAllPrimitiveId()) || []) {
  const got = await eda.pcb_PrimitivePour.get([id]);
  const one = Array.isArray(got) ? got[0] : got;
  if (!one) continue;
  pours.push({ net: one.getState_Net() || '', layer: one.getState_Layer() });
}
return { outline, comps, pads, lines, vias, pours };
"""


#: 读取所有器件属性（Value / Voltage Rating / Footprint / LCSC），供电路仿真使用。
_PROPS_JS = r"""
const comps = (await eda.pcb_PrimitiveComponent.getAll()) || [];
const out = [];
for (const c of comps) {
  const des = c.getState_Designator() || '';
  let prop = {};
  try { const raw = c.getState_OtherProperty(); prop = typeof raw === 'string' ? JSON.parse(raw) : (raw || {}); } catch (e) { }
  out.push({ des, prop });
}
return out;
"""

#: 这些 ``getAll()`` 在数据量大时非常慢（千条走线要几十秒），会连带把整个
#: ``/execute`` 拖过超时——表现出来就像"布线器返回 undefined"。
#: 只读计数/列举请改用对应的 ``getAllPrimitiveId()``（毫秒级）。
_SLOW_APIS: tuple[tuple[str, str], ...] = (
    ("pcb_PrimitiveLine.getAll(", "pcb_PrimitiveLine.getAllPrimitiveId()"),
    ("pcb_PrimitiveVia.getAll(", "pcb_PrimitiveVia.getAllPrimitiveId()"),
    ("pcb_PrimitivePad.getAll(", "pcb_PrimitivePad.getAllPrimitiveId()"),
    (
        "pcb_PrimitiveComponent.getAll(",
        "pcb_PrimitiveComponent.getAllPrimitiveId('part')",
    ),
    ("pcb_PrimitivePolyline.getAll(", "pcb_PrimitivePolyline.getAllPrimitiveId()"),
    ("pcb_PrimitivePour.getAll(", "pcb_PrimitivePour.getAllPrimitiveId()"),
)

#: 已知的长任务接口（几十秒 ~ 几分钟）：未显式给 timeout_seconds 时给出提醒。
_LONG_RUNNING_APIS: tuple[str, ...] = (
    "autoRouting",
    "autoLayout",
    "importChanges",
    "getGerberFile",
    "getBomFile",
    "getPickAndPlaceFile",
    "getIpc2581CFile",
)

#: 长任务建议的超时秒数。
_LONG_HINT_SECONDS = 300

#: 只在"看起来像函数调用"时才认定是长任务接口（``autoRouting(``）。
#: 用裸子串匹配会把字符串/注释里的名字也算进来——`return "autoRouting";`
#: 明明是瞬时调用，却会被抬到 300 秒超时并在结果里谎报 timeoutAutoRaised。
_LONG_RUNNING_CALL = re.compile(
    r"\b(?:" + "|".join(_LONG_RUNNING_APIS) + r")\s*\("
)


def _code_warnings(code: str, timeout_seconds: float | None) -> list[str]:
    """对提交的代码做静态体检，返回非阻塞的提醒。"""
    warnings: list[str] = []
    for needle, replacement in _SLOW_APIS:
        if needle in code:
            warnings.append(
                f"检测到 {needle.rstrip('(')}：它在图元多时可能耗时数十秒，"
                f"会把整个请求拖过超时；只读场景请改用 await {replacement}"
            )
    if timeout_seconds is None and _LONG_RUNNING_CALL.search(code):
        warnings.append(
            "检测到长任务接口（autoRouting / autoLayout / 导出类），未显式传 "
            f"timeout_seconds，已自动使用 {_LONG_HINT_SECONDS} 秒超时。"
            "若仍超时，不要重复提交同一任务——请求仍在 EasyEDA 内执行。"
        )
    return warnings


def _effective_timeout(
    code: str, timeout_seconds: float | None
) -> tuple[float | None, bool]:
    """决定本次请求真正使用的超时时间。

    显式传入的 ``timeout_seconds`` 永远优先；未传且代码里出现长任务接口时，
    自动提升到 :data:`protocol.LONG_REQUEST_TIMEOUT`（默认 300s），
    避免"自动布线本来就要几分钟，却必然按 30s 超时"的情况。
    """
    if timeout_seconds is not None:
        return timeout_seconds, False
    if _LONG_RUNNING_CALL.search(code):
        return P.LONG_REQUEST_TIMEOUT, True
    return None, False


def register_tools(mcp: MCPServer, client: BridgeClient, index: DocIndex) -> None:
    """把全部 EasyEDA 工具注册到 ``mcp`` 上。"""
    enum_index = EnumIndex(index.root)

    def _prepare_code(
        code: str, inject_enums: bool | None
    ) -> tuple[str, list[str], list[str]]:
        """按需为代码注入枚举常量定义。

        返回 ``(实际发送的代码, 已注入的枚举名, 被引用却无法注入的枚举名)``。

        * 代码里没有枚举引用时**一个字节都不改**——这是"原样转发"的底线；
        * ``inject_enums=False`` 时同样原样发送，并把引用到的枚举名回报给调用方，
          让它知道这些名字在网关作用域里会抛 ReferenceError。
        """
        if not isinstance(code, str):
            return code, [], []
        referenced = enum_index.detect(code)
        if not referenced:
            return code, [], []
        if inject_enums is False:
            return code, [], referenced
        prelude = enum_index.js_prelude(referenced)
        injected = [name for name in referenced if enum_index.members(name)]
        unresolved = [name for name in referenced if name not in injected]
        if not prelude:
            return code, [], referenced
        return prelude + code, injected, unresolved

    async def _call_with_reconnect(fn, *, retry_delivered: bool = False):
        """桥接换端口 / EDA 窗口重连时自动重试。

        重试有严格前提：**请求必须从未送达 EasyEDA**（``delivered=False``）。
        桥接服务换端口、窗口还没注册、显式指定的窗口暂时不在线，都属于这一类，
        重试是安全的。

        一旦请求已经送达（``delivered=True``），EasyEDA 里可能已经执行了这段
        代码——例如已经创建了器件、画了线。这时再重发一次会得到"位号和坐标完全
        相同的重复器件"，图上根本看不出来。所以默认**不重试**，只把错误原样上报，
        由调用方用只读查询确认实际结果。

        只读类操作（status / list / ping）可以显式传 ``retry_delivered=True``。
        """
        try:
            return await fn()
        except EdaNotConnectedError as first_exc:
            if first_exc.delivered and not retry_delivered:
                raise
            # 只有"桥接本身已经不在了"才需要重新发现端口。
            # 如果桥接还活着、只是暂时没有 EDA 窗口，绝不能去扫端口范围换个桥接——
            # 同一个端口范围内可能还跑着另一个桥接实例（以及另一台 EDA），
            # 那样代码会静默跑到另一个实例的窗口里。
            bridge_alive = True
            try:
                await client.health()
            except BridgeError:
                bridge_alive = False
            if not bridge_alive:
                found = await BridgeClient.discover(host=client.host)
                if found is not None and found.port and found.port != client.port:
                    logger.info(
                        "原桥接服务已不可达，迁移到端口 %s", found.port
                    )
                    client.port = found.port
            # 给 EDA 扩展一点时间重新 register（窗口 ID 在同标签页内保持不变）
            last_exc: BridgeError = first_exc
            for delay in (0.3, 0.7, 1.2, 2.0, 3.0):
                await asyncio.sleep(delay)
                try:
                    return await fn()
                except EdaNotConnectedError as exc:
                    if exc.delivered and not retry_delivered:
                        raise
                    last_exc = exc
            raise last_exc

    # ─── 状态与窗口 ─────────────────────────────────────────────────────

    @mcp.tool(
        name="eda_status",
        title="桥接与 EasyEDA 连接状态",
        description=(
            "检查桥接服务以及连接到它的 EasyEDA 专业版窗口。开始任何 EDA 操作之前、"
            "以及任何一次调用失败之后都应先调用它。返回 bridge.running（桥接是否在运行）、"
            "eda.activeWindowId（当前活动窗口）和 eda.windows（全部窗口列表）。"
        ),
    )
    async def eda_status() -> dict[str, Any]:
        base_url = client.base_url if client.known else None
        try:
            health = await _call_with_reconnect(lambda: client.health())
            windows = await _call_with_reconnect(lambda: client.list_windows())
        except BridgeError as exc:
            return {
                "bridge": {
                    "running": False,
                    "baseUrl": base_url,
                    "port": client.port,
                    "error": str(exc),
                },
                "eda": {
                    "connected": False,
                    "windowCount": 0,
                    "activeWindowId": None,
                    "windows": [],
                },
                "hint": (
                    "桥接服务当前不可达。请用 `python -m easyeda_mcp` 启动本 MCP 服务"
                    f"（桥接端口 {P.PORT_START}-{P.PORT_END}），然后再次调用 eda_status。"
                ),
            }

        connected = bool(health.get("edaConnected"))
        result: dict[str, Any] = {
            "bridge": {
                "running": True,
                "service": health.get("service"),
                "baseUrl": client.base_url,
                "port": client.port,
                "pendingRequests": health.get("pendingRequests", 0),
                "timestamp": health.get("timestamp"),
                "mcpSdk": MCP_SDK,
            },
            "eda": {
                "connected": connected,
                "windowCount": windows.get("count", health.get("edaWindowCount", 0)),
                "activeWindowId": windows.get("activeWindowId"),
                "windows": windows.get("windows", []),
            },
        }
        if not connected:
            result["hint"] = _HINT_NOT_CONNECTED
        elif windows.get("count", 0) > 1:
            result["hint"] = (
                "当前连接了多个 EasyEDA 窗口。调用 eda_execute 时请传 window_id 指定目标，"
                "或先调用 eda_select_window 设置默认窗口。"
            )
        return result

    @mcp.tool(
        name="eda_list_windows",
        title="列出 EasyEDA 窗口",
        description=(
            "列出当前连接到桥接服务的所有 EasyEDA 专业版窗口，并标出哪一个是活动窗口。"
        ),
    )
    async def eda_list_windows() -> dict[str, Any]:
        try:
            payload = await _call_with_reconnect(lambda: client.list_windows())
        except BridgeError as exc:
            return {"ok": False, "windows": [], "count": 0, "error": str(exc)}
        return {
            "ok": True,
            "windows": payload.get("windows", []),
            "activeWindowId": payload.get("activeWindowId"),
            "count": payload.get("count", 0),
        }

    @mcp.tool(
        name="eda_select_window",
        title="选择活动窗口",
        description=(
            "把某个已连接的 EasyEDA 窗口设为后续执行的默认目标窗口。"
            "窗口 ID 可通过 eda_list_windows 获取。"
        ),
    )
    async def eda_select_window(window_id: str) -> dict[str, Any]:
        try:
            payload = await _call_with_reconnect(lambda: client.select_window(window_id))
        except BridgeError as exc:
            return {"ok": False, "windowId": window_id, "error": str(exc)}
        return {
            "ok": True,
            "windowId": payload.get("activeWindowId", window_id),
            "message": f"当前活动 EasyEDA 窗口已切换为 {window_id}。",
        }

    # ─── 代码执行 ───────────────────────────────────────────────────────

    @mcp.tool(
        name="eda_execute",
        title="在 EasyEDA 中执行 JavaScript",
        description=(
            "在运行中的 EasyEDA 专业版客户端里执行 JavaScript，并返回其 `return` 的值。"
            "代码以 `async function(eda) { ... }` 的形式运行：`eda` 是 EasyEDA API 根对象，"
            "所有返回 Promise 的接口都必须 `await`，并且必须用 `return` 把结果送回来"
            "（`console.log` 的输出不会被捕获）。请使用枚举成员（例如 EPCB_LayerId.TOP）"
            "而不是裸数字；调用 PCB_*/SCH_* 接口前先确认当前文档类型。"
            "写代码之前请先用 eda_api_search / eda_api_doc 查证接口签名。"
            "\n\n【枚举】网关的执行作用域里**没有**枚举符号（`EPCB_LayerId` 直接引用会抛"
            " ReferenceError），因此本工具会自动把代码中用到的枚举常量前置注入"
            "（返回里 injectedEnums 列出注入项）；不想注入就传 inject_enums=false，"
            "改用 eda_enum 查到数值后写裸数字。"
            "\n\n【超时 ≠ 失败】返回 ok=false 且 timeout=true 时，**操作并没有失败**："
            "请求已送达 EasyEDA 并在其内部继续执行，只是本地不再等待。"
            "此时严禁立刻重提同一任务——布线类接口（autoRouting）每次调用都会先"
            "清空再用 isKeep 重布，重复提交等于把已有成果扔掉。"
            "正确做法：① 用轻量只读查询轮询真实状态；② 或调大 timeout_seconds 重提。"
            "\n\n【性能陷阱】`getAll()` 在大量图元时非常慢（千条走线要几十秒），"
            "会连带把请求拖超时；只读计数/列举请改用 `await X.getAllPrimitiveId()`（毫秒级）。"
            "\n\n【长任务】布线 / 自动布局 / Gerber·BOM 导出请显式传 "
            "timeout_seconds=300（或更大）；未显式指定时，检测到这些接口会自动"
            "提升到 300 秒（返回里 timeoutAutoRaised=true 表示走了这套默认值）。"
        ),
    )
    async def eda_execute(
        code: str,
        window_id: str | None = None,
        timeout_seconds: float | None = None,
        inject_enums: bool | None = None,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        wire_code, injected_enums, unresolved_enums = _prepare_code(code, inject_enums)
        warnings = _code_warnings(code, timeout_seconds)
        if unresolved_enums:
            warnings.append(
                "代码引用了网关作用域里不存在的枚举符号："
                f"{', '.join(unresolved_enums)}，执行会抛 ReferenceError。"
                "改用 eda_enum 查数值，或去掉 inject_enums=false 让本工具自动注入。"
            )
        effective_timeout, auto_raised = _effective_timeout(code, timeout_seconds)
        try:
            outcome: ExecutionOutcome = await _call_with_reconnect(
                lambda: client.execute(
                    wire_code, window_id=window_id, timeout=effective_timeout
                )
            )
        except BridgeError as exc:
            payload: dict[str, Any] = {
                "ok": False,
                "error": str(exc),
                "errorType": type(exc).__name__,
                "windowId": window_id,
                "durationMs": round((time.perf_counter() - started) * 1000, 1),
            }
            if isinstance(exc, EdaTimeoutError):
                # 关键：把"超时但仍在跑"说清楚，否则调用方会误判成失败并重试。
                payload.update(exc.to_payload())
                if effective_timeout is not None:
                    payload["timeoutSeconds"] = effective_timeout
                payload["note"] = (
                    "这不是失败：EasyEDA 仍在执行这段脚本。不要重复提交同一任务；"
                    "先用一段轻量查询（await eda.pcb_PrimitiveLine.getAllPrimitiveId()）"
                    "确认真实进度，再决定是否补做。"
                )
            elif isinstance(exc, EdaNotConnectedError):
                # delivered=False 才允许重试；为 True 时说明代码可能已经在 EDA 里执行过。
                payload["delivered"] = exc.delivered
                payload["availableWindows"] = exc.available
                if exc.delivered:
                    payload["retrySafe"] = False
                    payload["note"] = (
                        "请求已送达 EasyEDA，连接随后断开；EDA 内部可能已经执行了这段"
                        "代码。不要直接重提，先用只读查询确认是否已生效，否则会重复创建"
                        "器件/走线。"
                    )
                else:
                    payload["retrySafe"] = True
            if warnings:
                payload["warnings"] = warnings
            return payload
        result: dict[str, Any] = {
            "ok": True,
            "windowId": outcome.window_id,
            "result": json_safe(outcome.result),
            "durationMs": round((time.perf_counter() - started) * 1000, 1),
        }
        if auto_raised:
            result["timeoutSeconds"] = effective_timeout
            result["timeoutAutoRaised"] = True
        if injected_enums:
            result["injectedEnums"] = injected_enums
        if warnings:
            result["warnings"] = warnings
        return result

    @mcp.tool(
        name="eda_execute_batch",
        title="批量执行多段代码",
        description=(
            "一次调用中按顺序执行多段 JavaScript，减少往返次数。"
            '`steps` 是对象列表，每项形如 `{"name": "可选标签", "code": "return ...;"}`。'
            "各步骤在同一个 EasyEDA 窗口中依次执行，返回值中会给出每一步的结果。"
        ),
    )
    async def eda_execute_batch(
        steps: list[dict[str, Any]],
        window_id: str | None = None,
        stop_on_error: bool = False,
        timeout_seconds: float | None = None,
        inject_enums: bool | None = None,
    ) -> dict[str, Any]:
        if not steps:
            return {"ok": False, "error": "steps 不能为空", "results": []}

        results: list[dict[str, Any]] = []
        aborted = False
        for position, step in enumerate(steps):
            if not isinstance(step, dict) or not isinstance(step.get("code"), str):
                results.append(
                    {
                        "index": position,
                        "ok": False,
                        "error": '每一步都必须是包含 "code" 字符串的对象',
                    }
                )
                if stop_on_error:
                    aborted = True
                    break
                continue

            name = step.get("name")
            step_inject = step.get("inject_enums", inject_enums)
            wire_code, injected_enums, _unresolved = _prepare_code(
                step["code"], step_inject
            )
            step_timeout, auto_raised = _effective_timeout(
                step["code"], step.get("timeout_seconds", timeout_seconds)
            )
            started = time.perf_counter()
            try:
                outcome = await _call_with_reconnect(
                    lambda code=wire_code, t=step_timeout: client.execute(
                        code, window_id=window_id, timeout=t
                    )
                )
            except BridgeError as exc:
                row: dict[str, Any] = {
                    "index": position,
                    "name": name,
                    "ok": False,
                    "error": str(exc),
                    "errorType": type(exc).__name__,
                    "durationMs": round((time.perf_counter() - started) * 1000, 1),
                }
                if auto_raised:
                    row["timeoutSeconds"] = step_timeout
                    row["timeoutAutoRaised"] = True
                if isinstance(exc, EdaTimeoutError):
                    row.update(exc.to_payload())
                elif isinstance(exc, EdaNotConnectedError):
                    row["delivered"] = exc.delivered
                    row["availableWindows"] = exc.available
                    row["retrySafe"] = not exc.delivered
                results.append(row)
                # 连接类错误之后再多执行也没有意义，直接中断
                if stop_on_error or isinstance(exc, EdaNotConnectedError):
                    aborted = True
                    break
                continue

            row = {
                "index": position,
                "name": name,
                "ok": True,
                "windowId": outcome.window_id,
                "result": json_safe(outcome.result),
                "durationMs": round((time.perf_counter() - started) * 1000, 1),
            }
            if auto_raised:
                row["timeoutSeconds"] = step_timeout
                row["timeoutAutoRaised"] = True
            if injected_enums:
                row["injectedEnums"] = injected_enums
            if _unresolved:
                # 单步关掉注入时，这些枚举在网关作用域里不存在，执行必抛 ReferenceError
                row["warnings"] = [
                    "代码引用了网关作用域里不存在的枚举符号："
                    + ", ".join(_unresolved)
                    + "，执行会抛 ReferenceError。"
                ]
            results.append(row)

        executed = len(results)
        failed = sum(1 for row in results if not row.get("ok"))
        return {
            "ok": failed == 0 and not aborted,
            "executed": executed,
            "failed": failed,
            "aborted": aborted,
            "results": results,
        }

    @mcp.tool(
        name="eda_pcb_autoroute",
        title="分批自动布线（断点续跑）",
        description=(
            "把网络列表切成小片，逐片调用 eda.pcb_Document.autoRouting(...)，每片后保存检查点。"
            "为什么要分批：整板一次布线会跑 3~9 分钟，把 EasyEDA 渲染进程拖崩（页面重载、"
            "windowId 变化、在途请求丢失）。分片后每片 20~60 秒，既能完成布线又不会拖崩客户端；"
            "中途崩了也能用返回的 remainingNets 从断点继续。"
            "\n参数：nets（网络名数组）、chunk_size（每片网络数，默认 6，上限 20）、"
            "layers（默认 [1,2] 只用 Top/Bottom，把内层留给 GND/电源平面）、"
            "optimization（1=质量优先，0=快）、save_between_chunks（默认 true）、"
            "timeout_seconds（每片超时，默认 180 秒）。"
            "\n返回：ok、chunks（每片明细）、done（成功网络）、failedNets、remainingNets。"
        ),
    )
    async def eda_pcb_autoroute(
        nets: list[str],
        chunk_size: int = 6,
        layers: list[int] | None = None,
        optimization: int = 1,
        window_id: str | None = None,
        timeout_seconds: float = 180.0,
        save_between_chunks: bool = True,
    ) -> dict[str, Any]:
        cleaned: list[str] = []
        for net in nets or []:
            if isinstance(net, str) and net and net not in cleaned:
                cleaned.append(net)
        if not cleaned:
            return {"ok": False, "error": "nets 不能为空", "chunks": []}
        size = max(1, min(int(chunk_size), 20))
        layer_ids = layers or [1, 2]
        save_js = "await eda.pcb_Document.save();" if save_between_chunks else ""

        rows: list[dict[str, Any]] = []
        done: list[str] = []
        failed: list[str] = []
        remaining: list[str] = []
        stopped: str | None = None

        for index, start in enumerate(range(0, len(cleaned), size), start=1):
            chunk = cleaned[start : start + size]
            code = (
                f"const nets = {json.dumps(chunk, ensure_ascii=False)};"
                f"const r = await eda.pcb_Document.autoRouting({{RoutingNets: nets,"
                f" layers: {json.dumps(layer_ids)}, optimization: {int(optimization)},"
                f" existingPrimitiveMode: 'keep'}});"
                f"{save_js}"
                f"return r;"
            )
            started = time.perf_counter()
            try:
                outcome: ExecutionOutcome = await _call_with_reconnect(
                    lambda code=code: client.execute(
                        code, window_id=window_id, timeout=timeout_seconds
                    )
                )
            except BridgeError as exc:
                remaining = cleaned[start:]
                stopped = str(exc)
                rows.append(
                    {
                        "index": index,
                        "nets": chunk,
                        "ok": False,
                        "error": str(exc),
                        "errorType": type(exc).__name__,
                        "delivered": getattr(exc, "delivered", None),
                        "durationMs": round((time.perf_counter() - started) * 1000, 1),
                    }
                )
                break

            payload = outcome.result if isinstance(outcome.result, dict) else {}
            chunk_failed = payload.get("failedNets") or []
            rows.append(
                {
                    "index": index,
                    "nets": chunk,
                    "ok": True,
                    "durationMs": round((time.perf_counter() - started) * 1000, 1),
                    "result": json_safe(payload) if payload else json_safe(outcome.result),
                }
            )
            done.extend(net for net in chunk if net not in chunk_failed)
            failed.extend(chunk_failed)

        return {
            "ok": stopped is None and not failed,
            "chunkSize": size,
            "layers": layer_ids,
            "chunks": rows,
            "done": done,
            "failedNets": failed,
            "remainingNets": remaining,
            "stoppedBy": stopped,
        }

    @mcp.tool(
        name="eda_pcb_drc",
        title="PCB DRC 实时查看（事件驱动缓存）",
        description=(
            "读取当前 PCB 的 DRC 结果，并维护一个跨调用的实时缓存。"
            "为什么需要它：EasyEDA 4.1.78 没有 v4.2 才有的 startRealTimeDrc()"
            "（实测恒返回 false），而 pcb_Drc.check() 单次要 3~8 秒。本工具在 EDA 里"
            "注册事件监听并把结果缓存在扩展的 globalThis 中；改完电路再调用它，"
            "只有在'有变更/缓存过期'时才真的跑一次 check，其余时候毫秒级返回。"
            "\n\naction：status（默认，脏了或过期就自动刷新）、check（强制重跑）、"
            "watch（注册监听；auto=true 时变更后 1.5 秒自动重跑）、unwatch、"
            "diff（返回相对上次快照新增/修复的违规数与明细）、capabilities（只看版本与能力）。"
            "\n\nmode：auto（默认，按版本自动选择）、realtime（EDA>=4.2 时强制原生实时 DRC）、"
            "poll（强制降级为事件+主动检查）。低版本降级时会先作废旧结果再 check，"
            "保证返回的是当前 DRC 而不是上一次的缓存（clear_before_check=false 可关闭）。"
            "\ninclude_items=true 时附带违规明细（最多 limit 条）；ttl_seconds 控制"
            "缓存新鲜期（默认 5 秒）。返回里 source=cache 表示没重跑、refreshed 表示刚重跑。"
        ),
    )
    async def eda_pcb_drc(
        action: str = "status",
        include_items: bool = False,
        limit: int = 50,
        ttl_seconds: float = 5.0,
        auto: bool = False,
        force: bool = False,
        mode: str = "auto",
        clear_before_check: bool = True,
        window_id: str | None = None,
        timeout_seconds: float = 60.0,
    ) -> dict[str, Any]:
        if action not in DRC_ACTIONS:
            return {
                "ok": False,
                "action": action,
                "error": f"不支持的动作 {action!r}，可选：{', '.join(DRC_ACTIONS)}",
            }
        try:
            code = build_drc_js(
                action,
                include_items=include_items,
                limit=limit,
                ttl_ms=int(ttl_seconds * 1000),
                auto=auto,
                force=force,
                run_now=(action == "watch"),
                mode=mode,
                clear_before_check=clear_before_check,
            )
        except (TypeError, ValueError) as exc:
            return {"ok": False, "action": action, "error": str(exc)}

        started = time.perf_counter()
        try:
            outcome: ExecutionOutcome = await _call_with_reconnect(
                lambda: client.execute(
                    code, window_id=window_id, timeout=timeout_seconds
                ),
                retry_delivered=True,
            )
        except BridgeError as exc:
            payload: dict[str, Any] = {
                "ok": False,
                "action": action,
                "error": str(exc),
                "errorType": type(exc).__name__,
                "durationMs": round((time.perf_counter() - started) * 1000, 1),
            }
            if isinstance(exc, EdaTimeoutError):
                payload.update(exc.to_payload())
                payload["note"] = (
                    "这不是失败：EDA 仍在跑 check。稍后再次调用 action='status' 读取缓存即可。"
                )
            elif isinstance(exc, EdaNotConnectedError):
                payload["delivered"] = exc.delivered
                payload["retrySafe"] = not exc.delivered
            return payload

        data = normalize_drc_result(outcome.result)
        data["action"] = action
        data["windowId"] = outcome.window_id
        data["durationMs"] = round((time.perf_counter() - started) * 1000, 1)
        return data

    @mcp.tool(
        name="eda_pcb_channel_map",
        title="PCB 通道热力图与网络可达性",
        description=(
            "导出整板几何，在 Python 侧建 8 mil 双层栅格，用 flood fill 计算每个网络的"
            "焊盘之间是否连通（只走空格/同网络），并输出阻塞热力图。"
            "这是'看飞线判断哪堵'的算法化：几十毫秒就能回答'这个网络到底有没有路'，"
            "比跑一次 autoRouting（4~8 秒且可能返回 null）快两个数量级。"
        ),
    )
    async def eda_pcb_channel_map(
        gx_count: int = 24,
        gy_count: int = 16,
        window_id: str | None = None,
    ) -> dict[str, Any]:
        try:
            outcome = await _call_with_reconnect(
                lambda: client.execute(_GEOMETRY_JS, window_id=window_id, timeout=60.0),
                retry_delivered=True,
            )
        except BridgeError as exc:
            return {"ok": False, "error": str(exc), "errorType": type(exc).__name__}
        geom = outcome.result
        if not isinstance(geom, dict) or not geom.get("outline"):
            return {"ok": False, "error": "EDA 没有返回有效几何"}
        try:
            nets = sorted({p["net"] for p in geom.get("pads", []) if p.get("net")})
            status = {n: layout_advisor.net_connected(geom, n, set()) for n in nets}
            heat = layout_advisor.channel_heatmap(geom, int(gx_count), int(gy_count))
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"通道分析失败：{exc}"}
        return {
            "ok": True,
            "grid": {"resMil": layout_advisor.RES, "connected": status},
            "nets": status,
            "disconnected": [n for n, v in status.items() if not v],
            "heatmap": heat,
            "counts": {"pads": len(geom.get("pads", [])), "lines": len(geom.get("lines", [])), "vias": len(geom.get("vias", []))},
        }

    @mcp.tool(
        name="eda_pcb_blockers",
        title="反事实找挡路器件",
        description=(
            "对指定网络做反事实分析：临时在栅格上移除候选器件，看该网络是否从'不连通'"
            "变成'连通'。返回的 blockers 就是'挪开它就能通'的器件——这是'该挪谁'的算法化，"
            "不用先跑一遍真实布线再盲试。"
        ),
    )
    async def eda_pcb_blockers(
        net: str,
        radius: float = 150.0,
        window_id: str | None = None,
    ) -> dict[str, Any]:
        try:
            outcome = await _call_with_reconnect(
                lambda: client.execute(_GEOMETRY_JS, window_id=window_id, timeout=60.0),
                retry_delivered=True,
            )
        except BridgeError as exc:
            return {"ok": False, "error": str(exc), "errorType": type(exc).__name__}
        geom = outcome.result
        if not isinstance(geom, dict) or not geom.get("outline"):
            return {"ok": False, "error": "EDA 没有返回有效几何"}
        try:
            connected = layout_advisor.net_connected(geom, net, set())
            candidates = layout_advisor.nearby_components(geom, net, float(radius))
            found = [] if connected else layout_advisor.blockers(geom, net, candidates)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"反事实分析失败：{exc}"}
        return {
            "ok": True,
            "net": net,
            "connected": connected,
            "candidates": candidates,
            "blockers": found,
            "hint": (
                "该网络本来就连通，不需要移件。"
                if connected
                else ("移开 blockers 里的器件后该网络有通道。" if found else "近邻器件都不是唯一障碍，可能需要组合移动。")
            ),
        }

    @mcp.tool(
        name="eda_pcb_move_reroute",
        title="事务化移动器件并重布（失败回滚）",
        description=(
            "移动一个器件并只重布它**直接接触**的走线，用 EDA 的 DRC 做接受判据；变差则位置和走线一起回滚。"
            "顺序：快照（位置 + 该网络全部走线 + 焊盘几何）→ 只删与焊盘接触的线段 → 移动 → autoRouting → DRC 对比。"
            "注意不能按'该器件所有网络'删线：C1 挂在 GND 上时那等于删掉全板 344 段 GND/VCC5V 走线（实测过）。"
        ),
    )
    async def eda_pcb_move_reroute(
        des: str,
        x: float,
        y: float,
        rotation: float | None = None,
        window_id: str | None = None,
        timeout_seconds: float = 240.0,
    ) -> dict[str, Any]:
        snap_js = (
            "const des = " + json.dumps(des) + ";\n"
            "const comps = (await eda.pcb_PrimitiveComponent.getAll()) || [];\n"
            "const c = comps.find((v) => (v.getState_Designator() || '') === des);\n"
            "if (!c) return { error: 'component not found' };\n"
            "const cid = c.getState_PrimitiveId();\n"
            "let list = []; try { const raw = c.getState_Pads(); list = Array.isArray(raw) ? raw : JSON.parse(raw || '[]'); } catch (e) { }\n"
            "const nets = new Set(); const pads = [];\n"
            "for (const it of list) {\n"
            "  const p = await eda.pcb_PrimitivePad.get(cid + String(it.primitiveId));\n"
            "  if (!p) continue;\n"
            "  const net = p.getState_Net() || '';\n"
            "  if (net) nets.add(net);\n"
            "  let d = null; try { d = p.getState_Pad(); if (typeof d === 'string') d = JSON.parse(d); } catch (e) { }\n"
            "  const shape = Array.isArray(d) ? String(d[0] || '') : 'RECT';\n"
            "  const w = Array.isArray(d) ? Number(d[1]) || 0 : 0;\n"
            "  const h = Array.isArray(d) ? Number(d[2]) || 0 : 0;\n"
            "  pads.push({ net, x: p.getState_X(), y: p.getState_Y(), rot: p.getState_Rotation(), shape, w, h });\n"
            "}\n"
            "const lines = [];\n"
            "for (const id of (await eda.pcb_PrimitiveLine.getAllPrimitiveId()) || []) {\n"
            "  const got = await eda.pcb_PrimitiveLine.get([id]);\n"
            "  const one = Array.isArray(got) ? got[0] : got;\n"
            "  if (one && nets.has(one.getState_Net())) lines.push({ id, net: one.getState_Net(), layer: one.getState_Layer(), x1: one.getState_StartX(), y1: one.getState_StartY(), x2: one.getState_EndX(), y2: one.getState_EndY(), width: one.getState_LineWidth() });\n"
            "}\n"
            "return { x: c.getState_X(), y: c.getState_Y(), rot: c.getState_Rotation(), nets: [...nets], pads, lines };\n"
        )
        try:
            snap = (await client.execute(snap_js, window_id=window_id, timeout=90.0)).result
        except BridgeError as exc:
            return {"ok": False, "error": str(exc), "errorType": type(exc).__name__}
        if not isinstance(snap, dict) or snap.get("error"):
            return {"ok": False, "error": (snap or {}).get("error", "快照失败")}
        all_lines = snap.get("lines") or []
        pads = snap.get("pads") or []
        nets = snap.get("nets") or []

        # 只挑"与器件焊盘几何接触"的线段（接触容差 1 mil）
        def touches(line: dict) -> bool:
            for p in pads:
                if not p.get("w") and not p.get("h"):
                    continue
                if layout_advisor.seg_pad(line["x1"], line["y1"], line["x2"], line["y2"], p) <= 1.0:
                    return True
            return False

        contacts = [l for l in all_lines if touches(l)]
        contact_ids = [l["id"] for l in contacts]
        contact_nets = sorted({l["net"] for l in contacts})
        if not contact_ids:
            return {
                "ok": False,
                "error": "没有找到与该器件焊盘直接接触的走线；为避免误删整段网络，拒绝执行",
                "des": des,
                "nets": nets,
            }

        async def drc_total() -> int | None:
            js = (
                "const raw = await eda.pcb_Drc.check(true, false, true);\n"
                "let n = 0;\n"
                "for (const g of raw || []) for (const s of (g && g.list) || []) for (const it of (s && s.list) || []) n++;\n"
                "return n;\n"
            )
            try:
                return (await client.execute(js, window_id=window_id, timeout=240.0)).result
            except BridgeError:
                return None

        before = await drc_total()

        # ① 只删接触线
        del_js = (
            "const ids = " + json.dumps(contact_ids) + ";\n"
            "if (ids.length) await eda.pcb_PrimitiveLine.delete(ids);\n"
            "await eda.pcb_Document.save();\n"
            "return ids.length;\n"
        )
        try:
            deleted = (await client.execute(del_js, window_id=window_id, timeout=120.0)).result
        except BridgeError as exc:
            return {"ok": False, "error": f"删除接触走线失败：{exc}"}

        # ② 移动
        move_js = (
            "const des = " + json.dumps(des) + ";\n"
            "const comps = (await eda.pcb_PrimitiveComponent.getAll()) || [];\n"
            "const c = comps.find((v) => (v.getState_Designator() || '') === des);\n"
            "if (!c) return false;\n"
            "const prop = { x: " + repr(float(x)) + ", y: " + repr(float(y)) + " };\n"
            + ("prop.rotation = " + repr(float(rotation)) + ";\n" if rotation is not None else "")
            + "await eda.pcb_PrimitiveComponent.modify(c.getState_PrimitiveId(), prop);\n"
            + "await eda.pcb_Document.save();\n"
            "return true;\n"
        )
        try:
            await client.execute(move_js, window_id=window_id, timeout=60.0)
        except BridgeError as exc:
            return {"ok": False, "error": str(exc)}

        # ③ 只重布接触线所在的网络
        rows = []
        for i in range(0, len(contact_nets), 4):
            chunk = contact_nets[i : i + 4]
            route_js = (
                "const nets = " + json.dumps(chunk, ensure_ascii=False) + ";\n"
                "const r = await eda.pcb_Document.autoRouting({ RoutingNets: nets, layers: [1, 2], optimization: 1, existingPrimitiveMode: 'keep' });\n"
                "await eda.pcb_Document.save();\n"
                "return r;\n"
            )
            try:
                r = (await client.execute(route_js, window_id=window_id, timeout=timeout_seconds)).result
            except BridgeError as exc:
                r = {"error": str(exc)}
            rows.append({"nets": chunk, "result": r})

        after = await drc_total()
        regressed = before is not None and after is not None and after > before

        if regressed:
            # 回滚：删这些网络的现有线 → 恢复位置 → 重建全部快照走线
            back_js = (
                "const des = " + json.dumps(des) + ";\n"
                "const nets = new Set(" + json.dumps(nets, ensure_ascii=False) + ");\n"
                "const comps = (await eda.pcb_PrimitiveComponent.getAll()) || [];\n"
                "const c = comps.find((v) => (v.getState_Designator() || '') === des);\n"
                "if (!c) return false;\n"
                "await eda.pcb_PrimitiveComponent.modify(c.getState_PrimitiveId(), { x: " + repr(float(snap["x"])) + ", y: " + repr(float(snap["y"])) + ", rotation: " + repr(float(snap["rot"])) + " });\n"
                "const kill = [];\n"
                "for (const id of (await eda.pcb_PrimitiveLine.getAllPrimitiveId()) || []) {\n"
                "  const got = await eda.pcb_PrimitiveLine.get([id]);\n"
                "  const one = Array.isArray(got) ? got[0] : got;\n"
                "  if (one && nets.has(one.getState_Net())) kill.push(id);\n"
                "}\n"
                "if (kill.length) await eda.pcb_PrimitiveLine.delete(kill);\n"
                "const SEGS = " + json.dumps(all_lines, ensure_ascii=False) + ";\n"
                "for (const s of SEGS) { try { await eda.pcb_PrimitiveLine.create(s.net, s.layer, s.x1, s.y1, s.x2, s.y2, s.width || 8, false); } catch (e) { } }\n"
                "await eda.pcb_Document.save();\n"
                "return true;\n"
            )
            try:
                await client.execute(back_js, window_id=window_id, timeout=180.0)
            except BridgeError:
                pass
            restored = await drc_total()
            return {
                "ok": False,
                "rolledBack": True,
                "des": des,
                "drcBefore": before,
                "drcAfter": after,
                "drcAfterRollback": restored,
                "rollbackVerified": restored == before,
                "nets": nets,
                "contactNets": contact_nets,
                "contactLines": len(contact_ids),
                "deletedOldLines": deleted,
                "routing": rows,
                "note": "移动后 DRC 变差，已回滚；rollbackVerified=true 表示 DRC 恢复到移动前",
            }
        return {
            "ok": True,
            "rolledBack": False,
            "des": des,
            "from": {"x": snap["x"], "y": snap["y"], "rot": snap["rot"]},
            "to": {"x": x, "y": y, "rot": rotation},
            "nets": nets,
            "contactNets": contact_nets,
            "contactLines": len(contact_ids),
            "oldLineCount": len(all_lines),
            "deletedOldLines": deleted,
            "routing": rows,
            "drcBefore": before,
            "drcAfter": after,
            "rollbackVerified": None,
            "note": "ok=true 表示移动后 DRC 没有变差",
        }


    @mcp.tool(
        name="eda_pcb_circuit_sim",
        title="电路物理仿真（电源树/纹波/PDN/IR drop/降额）",
        description=(
            "纯 Python 物理仿真，不依赖 numpy/scipy：从 EDA 读取器件 Value 与额定电压，"
            "计算电源树设定点（buck 分压/LDO）、buck 输出纹波、PDN 阻抗（C+ESR+ESL 并联，"
            "1k~100MHz 扫频）、铜皮 IR drop 估算、以及每个元件的降额率（电压/功率/电流 vs 额定）。"
            "返回逐项检查与 summary.failed，可直接作为'能否投板'的依据之一。"
        ),
    )
    async def eda_pcb_circuit_sim(window_id: str | None = None) -> dict[str, Any]:
        try:
            outcome = await _call_with_reconnect(
                lambda: client.execute(_PROPS_JS, window_id=window_id, timeout=90.0),
                retry_delivered=True,
            )
        except BridgeError as exc:
            return {"ok": False, "error": str(exc), "errorType": type(exc).__name__}
        comps = outcome.result
        if not isinstance(comps, list) or not comps:
            return {"ok": False, "error": "EDA 没有返回器件属性"}
        items = [
            {
                "des": c.get("des", ""),
                "value": (c.get("prop") or {}).get("Value", ""),
                "voltageRating": (c.get("prop") or {}).get("Voltage Rating", ""),
                "footprint": (c.get("prop") or {}).get("Footprint", ""),
                "lcsc": (c.get("prop") or {}).get("LCSC Part Name", ""),
            }
            for c in comps
        ]
        try:
            result = circuit_sim.simulate(items)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"仿真计算失败：{exc}"}
        # MNA 全板仿真（DC + 逐轨 AC/瞬态）；失败不影响解析式结果
        try:
            geom = (await _call_with_reconnect(
                lambda: client.execute(_GEOMETRY_JS, window_id=window_id, timeout=90.0),
                retry_delivered=True,
            )).result
            if isinstance(geom, dict) and geom.get("pads"):
                result["mna"] = netlist_builder.run_full(geom["pads"], items)
        except Exception as exc:  # noqa: BLE001
            result["mnaError"] = str(exc)
        result["ok"] = True
        result["componentCount"] = len(items)
        return result

    @mcp.tool(
        name="eda_ping",
        title="探测 EasyEDA 是否在线",
        description=(
            "通过桥接服务往返执行一段极简代码，用来确认已连接的 EasyEDA 窗口真的有响应，"
            "并测量一次往返的耗时（毫秒）。"
        ),
    )
    async def eda_ping(window_id: str | None = None) -> dict[str, Any]:
        started = time.perf_counter()
        try:
            outcome = await _call_with_reconnect(
                lambda: client.ping(window_id=window_id), retry_delivered=True
            )
        except BridgeError as exc:
            return {"ok": False, "error": str(exc), "errorType": type(exc).__name__}
        return {
            "ok": True,
            "windowId": outcome.window_id,
            "roundTripMs": round((time.perf_counter() - started) * 1000, 1),
            "result": json_safe(outcome.result),
        }

    # ─── 枚举 ───────────────────────────────────────────────────────────

    @mcp.tool(
        name="eda_enum",
        title="查询 EasyEDA 枚举取值",
        description=(
            "查询 EasyEDA 专业版的枚举成员与数值（数据来自 references/enums/）。"
            "不传 name 时列出全部枚举名；传 name（大小写不敏感）返回成员→取值映射，"
            "以及可直接粘进代码的 JS 常量定义。"
            "\n\n为什么需要它：Run API Gateway 用 `new AsyncFunction('eda', code)` 执行代码，"
            "函数体只在全局作用域求值，而枚举常量在扩展包模块作用域里——"
            "所以 `EPCB_LayerId.TOP` 这类写法通过网关执行会抛 ReferenceError。"
            "eda_execute 会自动注入用到的枚举；想自己写裸数字时用本工具查准确数值。"
        ),
    )
    async def eda_enum(name: str | None = None) -> dict[str, Any]:
        if not name:
            names = enum_index.names()
            return {
                "ok": True,
                "count": len(names),
                "names": names,
                "note": "传 name 查看成员数值，例如 eda_enum(name='EPCB_LayerId')",
            }
        canonical = enum_index.resolve(name)
        if canonical is None:
            return {
                "ok": False,
                "name": name,
                "error": f"未找到枚举 {name}；可用 eda_enum() 列出全部枚举名",
            }
        members = enum_index.members(canonical)
        return {
            "ok": True,
            "name": canonical,
            "count": len(members),
            "members": members,
            "js": f"const {canonical} = {enum_index.js_literal(canonical)};",
        }

    # ─── 文档 ───────────────────────────────────────────────────────────

    @mcp.tool(
        name="eda_api_search",
        title="检索 EasyEDA API 文档",
        description=(
            "在随项目分发的 EasyEDA 专业版 API 文档（references/、format/、guide/）中，"
            "按类名、方法名或任意关键字检索，返回命中的文件与行。"
            "需要完整内容时再用 eda_api_doc 读取。请用它代替凭记忆猜测接口签名。"
        ),
    )
    async def eda_api_search(query: str, limit: int = 20) -> dict[str, Any]:
        try:
            return {"ok": True, **index.search(query, limit=limit)}
        except DocError as exc:
            return {"ok": False, "error": str(exc), "query": query}

    @mcp.tool(
        name="eda_api_doc",
        title="读取 EasyEDA API 文档",
        description=(
            "读取 references/、format/、guide/、user-guide/ 下的某一个文档文件"
            "（路径相对于项目根目录）。返回带行号的内容；长文档可用 offset/limit 翻页。"
        ),
    )
    async def eda_api_doc(path: str, offset: int = 0, limit: int = 200) -> dict[str, Any]:
        try:
            return {"ok": True, **index.read(path, offset=offset, limit=limit)}
        except DocError as exc:
            return {"ok": False, "error": str(exc), "path": path}
