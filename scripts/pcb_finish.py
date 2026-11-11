"""PCB 收尾流水线（通用版，不绑定任何具体工程）。

设计原则（都是实践踩出来的）：

1. **不写死任何 UUID / 网络名 / 层号**：PCB 从当前工程自动发现，平面层取内层，
   电源网络默认按名字前缀识别（`--power-nets` 可覆盖）。
2. **绝不整板一次布线**：EasyEDA 渲染进程跑长任务会 OOM 重载，一律按网络切片、
   每片 `save()`，中断可从剩余网络续跑。
3. **装配间距是硬指标**：`assemble` 给出两两铜-铜最小间距；`spread` 按系数外扩器件
   并等比放大板框（仍可控制在常见价格档内），把间距拉到手贴/风枪的安全区。
4. **细间距焊盘不打过孔**：0.5mm 间距的 QFP/HDMI 焊盘上放 12/24mil 孔必然孔-孔违规；
   先判邻焊盘中心距，放不下的改走"短扇出 + 空处打孔"，扇出线要做逐点避让。
5. **先布线后覆铜**，覆铜建完必须 `rebuildCopperRegion()`。

用法::

    python pcb_finish.py status                 # 只读：DRC 分类 + 未连通网络
    python pcb_finish.py assemble               # 只读：装配间距 + DRC 体检
    python pcb_finish.py spread --factor 1.45    # 器件外扩 + 板框等比放大（会清布线/覆铜）
    python pcb_finish.py route --chunk 5        # 补未连通网络（信号优先）
    python pcb_finish.py power                  # 建电源/地平面（区域重叠自动跳过）
    python pcb_finish.py stitch --nets GND,VCC3V3
    python pcb_finish.py silk --apply           # 位号丝印重排
    python pcb_finish.py drc                    # 0 违规时退出码 0
    python pcb_finish.py all                    # 全流程
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request

BRIDGE = "http://127.0.0.1:49620"
PLANE_LAYER_PREFERENCE = (15, 16, 17, 18)  # 4/6 层板的内层编号惯例：15,16 是 Inner1/Inner2


# ─── 传输 ────────────────────────────────────────────────────────────


def run_js(code: str, timeout: float = 240.0) -> dict:
    body = json.dumps({"code": code, "timeout": timeout}, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        BRIDGE + "/execute",
        data=body,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout + 40) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            return json.loads(exc.read().decode("utf-8"))
        except Exception:  # noqa: BLE001
            return {"success": False, "error": f"HTTP {exc.code}"}
    except Exception as exc:  # noqa: BLE001
        return {"success": False, "error": f"{type(exc).__name__}: {exc}"}


def payload(code: str, timeout: float = 240.0):
    result = run_js(code, timeout)
    if not result.get("success"):
        return None, str(result.get("error"))
    return result.get("result"), None


# ─── 通用发现：PCB / 板框 / 网络 ─────────────────────────────────────

DISCOVER_JS = """
const doc = await eda.dmt_SelectControl.getCurrentDocumentInfo();
let pcbUuid = null;
if (doc && doc.documentType === 3) pcbUuid = doc.uuid;
if (!pcbUuid) {
  const project = await eda.dmt_Project.getCurrentProjectInfo();
  for (const b of (project && project.data) || []) {
    if (b && b.pcb && b.pcb.uuid) { pcbUuid = b.pcb.uuid; break; }
  }
}
return { pcbUuid, currentType: doc ? doc.documentType : null };
"""

BOARD_JS = """
const layers = await eda.pcb_Layer.getAllLayers();
const copper = (layers || []).filter((l) => l.type === 'SIGNAL').map((l) => ({ id: l.id, name: l.name }));
let outline = [];
for (const id of (await eda.pcb_PrimitivePolyline.getAllPrimitiveId()) || []) {
  const got = await eda.pcb_PrimitivePolyline.get([id]);
  const one = Array.isArray(got) ? got[0] : got;
  if (one && one.getState_Layer() === 11) outline.push(one.getState_Polygon().polygon);
}
const polys = outline.filter((p) => Array.isArray(p) && p.length > 4);
const xs = [], ys = [];
for (const p of polys) for (let i = 0; i < p.length - 1; i += 2) { if (typeof p[i] === 'number') { xs.push(p[i]); ys.push(p[i + 1]); } }
return {
  copperLayers: copper,
  outlineCount: polys.length,
  bbox: xs.length ? { x0: Math.min(...xs), x1: Math.max(...xs), y0: Math.min(...ys), y1: Math.max(...ys) } : null,
  nets: await eda.pcb_Net.getAllNetsName(),
};
"""


def discover_pcb() -> str | None:
    data, error = payload(DISCOVER_JS, 60)
    if data is None:
        print(f"发现 PCB 失败：{error[:120]}")
        return None
    uuid = data.get("pcbUuid")
    if not uuid:
        print("当前工程里找不到 PCB 文档")
    return uuid


def board_info(open_js: str) -> dict:
    data, error = payload(open_js + BOARD_JS, 120)
    if data is None:
        return {}
    return data


def open_js_for(uuid: str) -> str:
    return (
        f"await eda.dmt_EditorControl.openDocument('{uuid}');"
        "await new Promise(r => setTimeout(r, 2000));"
    )


# ─── DRC / 体检 ──────────────────────────────────────────────────────

DRC_JS = """
const raw = await eda.pcb_Drc.check(true, false, true);
const items = [];
(function walk(n){ if(!n) return; if(Array.isArray(n)){for(const x of n) walk(x); return;}
  if(n.errorType){items.push(n); return;} if(Array.isArray(n.list)) walk(n.list); })(raw);
const byType = {}, unconnected = {};
for (const it of items) {
  const k = (it.errorType||'?') + ' | ' + (it.errorObjType||'?');
  byType[k] = (byType[k]||0)+1;
  if (String(it.errorType||'').includes('Connection')) {
    const net = it.net || 'unknown';
    unconnected[net] = (unconnected[net]||0)+1;
  }
}
return { total: items.length, byType, unconnected,
  lines: (await eda.pcb_PrimitiveLine.getAllPrimitiveId())?.length ?? null,
  vias: (await eda.pcb_PrimitiveVia.getAllPrimitiveId())?.length ?? null,
  pours: ((await eda.pcb_PrimitivePour.getAll()) || []).length };
"""


def print_drc(open_js: str, label: str) -> dict:
    data, error = payload(open_js + DRC_JS, 300)
    if data is None:
        print(f"[{label}] DRC 读取失败：{error[:110]}")
        return {}
    print(
        f"[{label}] DRC={data['total']}  走线={data['lines']}  过孔={data['vias']}  覆铜={data['pours']}"
    )
    for key, count in sorted(data["byType"].items(), key=lambda kv: -kv[1]):
        print(f"          {count:5d}  {key}")
    return data


ASSEMBLE_JS = """
const MIL2MM = 0.0254;
const comps = await eda.pcb_PrimitiveComponent.getAll();
const info = new Map();
for (const c of comps) info.set(c.getState_PrimitiveId(), c.getState_Designator() || '');
const pads = await eda.pcb_PrimitivePad.getAll();
const box = new Map();
for (const p of pads) {
  const cid = String(p.getState_PrimitiveId()).slice(0, 16);
  const b = box.get(cid) || { minX: 1e9, maxX: -1e9, minY: 1e9, maxY: -1e9 };
  const x = p.getState_X(), y = p.getState_Y();
  b.minX = Math.min(b.minX, x); b.maxX = Math.max(b.maxX, x);
  b.minY = Math.min(b.minY, y); b.maxY = Math.max(b.maxY, y);
  box.set(cid, b);
}
const ids = [...box.keys()];
const gaps = [], tight = [];
for (let i = 0; i < ids.length; i++) for (let j = i + 1; j < ids.length; j++) {
  const A = box.get(ids[i]), B = box.get(ids[j]);
  const g = Math.max(A.minX - B.maxX, B.minX - A.maxX, A.minY - B.maxY, B.minY - A.maxY);
  gaps.push(g);
  if (g < 80) tight.push({ a: info.get(ids[i]), b: info.get(ids[j]), mm: Math.round(g * MIL2MM * 100) / 100 });
}
gaps.sort((a, b) => a - b);
const raw = await eda.pcb_Drc.check(true, false, true);
const items = [];
(function walk(n){ if(!n) return; if(Array.isArray(n)){for(const x of n) walk(x); return;}
  if(n.errorType){items.push(n); return;} if(Array.isArray(n.list)) walk(n.list); })(raw);
const byType = {};
for (const it of items) { const k = (it.errorType||'?') + ' | ' + (it.errorObjType||'?'); byType[k] = (byType[k]||0)+1; }
return { components: ids.length, minGapMil: Math.round(gaps[0]*10)/10,
  minGapMm: Math.round(gaps[0]*MIL2MM*100)/100,
  under: { lt40: gaps.filter(g=>g<40).length, lt60: gaps.filter(g=>g<60).length, lt80: gaps.filter(g=>g<80).length },
  tightPairs: tight.sort((p,q)=>p.mm-q.mm).slice(0, 8),
  drc: items.length, byType,
  lines: (await eda.pcb_PrimitiveLine.getAllPrimitiveId())?.length ?? null,
  vias: (await eda.pcb_PrimitiveVia.getAllPrimitiveId())?.length ?? null,
  pours: ((await eda.pcb_PrimitivePour.getAll()) || []).length };
"""


def assemble(open_js: str) -> dict:
    data, error = payload(open_js + ASSEMBLE_JS, 300)
    if data is None:
        print(f"[assemble] 失败：{error[:110]}")
        return {}
    print(
        f"[assemble] 器件 {data['components']}，最小铜-铜间距 {data['minGapMm']}mm"
        f"（<1.0mm:{data['under']['lt40']} 对，<1.5mm:{data['under']['lt60']} 对，<2.0mm:{data['under']['lt80']} 对）"
    )
    if data["tightPairs"]:
        print("          最紧几对：", json.dumps(data["tightPairs"], ensure_ascii=False))
    print(f"          DRC {data['drc']} 项，走线 {data['lines']}，过孔 {data['vias']}，覆铜 {data['pours']}")
    for key, count in sorted((data["byType"] or {}).items(), key=lambda kv: -kv[1]):
        print(f"          {count:5d}  {key}")
    return data


# ─── 布线（切片） ────────────────────────────────────────────────────


def route_nets(open_js: str, nets: list[str], chunk: int, layers: list[int], opt: int, passes: int) -> None:
    pending = list(dict.fromkeys(nets))
    for attempt in range(1, passes + 1):
        if not pending:
            return
        print(f"\n== 第 {attempt} 轮：{len(pending)} 个网络，每片 {chunk} ==")
        failed: list[str] = []
        for start in range(0, len(pending), chunk):
            batch = pending[start : start + chunk]
            js = (
                open_js
                + f"const nets = {json.dumps(batch, ensure_ascii=False)};"
                + "const r = await eda.pcb_Document.autoRouting({RoutingNets: nets,"
                + f" layers: {json.dumps(layers)}, optimization: {opt},"
                + " existingPrimitiveMode: 'keep'});"
                + "await eda.pcb_Document.save();"
                + "return r;"
            )
            started = time.time()
            data, error = payload(js, 300)
            took = round(time.time() - started, 1)
            if data is None:
                # autoRouting 可能返回 undefined（success=true 但 result=null），说明这一组布不通；
                # 跳过它继续布其它组，不要整轮中止（否则电源组会挡住所有信号组）
                reason = (error or "布线器返回空结果（该组网络未能布通）")[:80]
                print(f"  {batch} 失败（{took}s）：{reason}")
                failed.extend(batch)
                continue
            bad = data.get("failedNets") or []
            print(f"  {took}s  {data.get('successNetsCount')}/{data.get('totalNetsCount')}  失败={bad}")
            failed.extend(bad)
        pending = [n for n in pending if n in failed]


# ─── 电源/地平面 ─────────────────────────────────────────────────────


def power_planes(open_js: str, power_nets: list[str]) -> None:
    js = (
        open_js
        + f"const POWER_NETS = {json.dumps(power_nets, ensure_ascii=False)};"
        + """
const PAD = 80, MARGIN = 30;
const pads = await eda.pcb_PrimitivePad.getAll();
const byNet = new Map();
for (const p of pads) {
  const net = p.getState_Net();
  if (!POWER_NETS.includes(net)) continue;
  const b = byNet.get(net) || { minX: 1e9, maxX: -1e9, minY: 1e9, maxY: -1e9 };
  const x = p.getState_X(), y = p.getState_Y();
  b.minX = Math.min(b.minX, x); b.maxX = Math.max(b.maxX, x);
  b.minY = Math.min(b.minY, y); b.maxY = Math.max(b.maxY, y);
  byNet.set(net, b);
}
let board = null;
for (const id of (await eda.pcb_PrimitivePolyline.getAllPrimitiveId()) || []) {
  const got = await eda.pcb_PrimitivePolyline.get([id]);
  const one = Array.isArray(got) ? got[0] : got;
  if (one && one.getState_Layer() === 11) { board = one.getState_Polygon().polygon; break; }
}
const xs = [], ys = [];
for (let i = 0; i < (board||[]).length - 1; i += 2) { if (typeof board[i] === 'number') { xs.push(board[i]); ys.push(board[i+1]); } }
const bx0 = Math.min(...xs)+MARGIN, bx1 = Math.max(...xs)-MARGIN, by0 = Math.min(...ys)+MARGIN, by1 = Math.max(...ys)-MARGIN;

const existing = ((await eda.pcb_PrimitivePour.getAll()) || []).map((p) => ({ net: p.getState_Net(), layer: p.getState_Layer() }));
const layers = await eda.pcb_Layer.getAllLayers();
const inner = (layers||[]).filter((l) => l.type === 'SIGNAL' && l.id >= 15).map((l) => l.id);
const planeLayer = (net) => (net === 'GND' ? (inner[0] || 15) : (inner[1] || inner[0] || 16));

const order = [...POWER_NETS].sort((a, b) => (a === 'GND' ? -1 : b === 'GND' ? 1 : 0));
const made = [], planned = [];
for (const net of order) {
  const b = byNet.get(net);
  if (!b) { made.push({ net, skipped: 'no-pads' }); continue; }
  if (existing.some((e) => e.net === net)) { made.push({ net, skipped: 'already' }); continue; }
  const x0 = Math.max(bx0, b.minX-PAD), x1 = Math.min(bx1, b.maxX+PAD);
  const y0 = Math.max(by0, b.minY-PAD), y1 = Math.min(by1, b.maxY+PAD);
  const layer = planeLayer(net);
  // 只有同一层上的区域重叠才冲突；不同内层可以叠放
  if (planned.some((q) => q.layer === layer && x0 < q.x1 && x1 > q.x0 && y0 < q.y1 && y1 > q.y0)) { made.push({ net, skipped: 'region-overlap' }); continue; }
  planned.push({ layer, x0, x1, y0, y1 });
  const poly = eda.pcb_MathPolygon.createPolygon([x0, y0, 'L', x0, y1, x1, y1, x1, y0, x0, y0]);
  const pour = await eda.pcb_PrimitivePour.create(net, planeLayer(net), eda.pcb_MathPolygon.createComplexPolygon([poly]));
  if (pour && pour.rebuildCopperRegion) await pour.rebuildCopperRegion();
  made.push({ net, layer: planeLayer(net), ok: !!pour });
}
await eda.pcb_Document.save();
return { made, layers: inner };
"""
    )
    data, error = payload(js, 600)
    print(f"[power] {data if data else error}")


# ─── 缝合 / 扇出（通用：焊盘内过孔 + 空处扇出） ──────────────────────


STITCH_JS = """
const NETS = __NETS__;
const HOLE = __HOLE__, DIA = __DIA__, TRACK_W = 6;
const PAD_CLEAR = 12, VIA_CLEAR = 30, MIN_PITCH = 55;

const pads = await eda.pcb_PrimitivePad.getAll();
const compOf = new Map();
const centers = new Map();
for (const c of await eda.pcb_PrimitiveComponent.getAll()) {
  const des = c.getState_Designator() || '';
  compOf.set(c.getState_PrimitiveId(), des);
  centers.set(des, { x: c.getState_X(), y: c.getState_Y() });
}
const all = [], own = [], blockers = [];
for (const p of pads) {
  const row = { net: p.getState_Net() || '', type: p.getState_PadType(),
    x: p.getState_X(), y: p.getState_Y(),
    des: compOf.get(String(p.getState_PrimitiveId()).slice(0, 16)) || '' };
  all.push(row);
  if (NETS.includes(row.net) && row.type !== 1) own.push(row);
  else if (row.net && !NETS.includes(row.net)) blockers.push(row);
}
function pitchFine(pad) {
  for (const q of all) {
    if (q === pad) continue;
    if (q.net === pad.net) continue;
    if (Math.abs(q.x - pad.x) < MIN_PITCH && Math.abs(q.y - pad.y) < MIN_PITCH) return true;
  }
  return false;
}
// 线-矩形相交（扇出线不能压到异网焊盘）
function segmentHitsPad(x0, y0, x1, y1, pad) {
  const half = 14;
  for (let t = 0; t <= 1.0001; t += 0.2) {
    const x = x0 + (x1 - x0) * t, y = y0 + (y1 - y0) * t;
    if (Math.abs(x - pad.x) < half && Math.abs(y - pad.y) < half) return true;
  }
  return false;
}

const vias = ((await eda.pcb_PrimitiveVia.getAll()) || []).map((v) => ({ x: v.getState_X(), y: v.getState_Y() }));
let made = 0, inPad = 0, fanout = 0, skipped = 0;
const added = [];

for (const pad of own) {
  // 1) 焊盘内过孔（细间距焊盘放不下，孔会碰到邻孔）
  if (!pitchFine(pad)) {
    try {
      const v = await eda.pcb_PrimitiveVia.create(pad.net, pad.x, pad.y, HOLE, DIA);
      if (v) { made += 1; inPad += 1; added.push({ x: pad.x, y: pad.y }); vias.push({ x: pad.x, y: pad.y }); }
      else skipped += 1;
    } catch (e) { skipped += 1; }
    continue;
  }
  // 2) 细间距焊盘：沿"离开器件中心"的径向短扇出（焊盘阵列的天然通道），到阵列外再打孔
  let done = false;
  const cc = centers.get(pad.des) || { x: pad.x, y: pad.y };
  let dx = pad.x - cc.x, dy = pad.y - cc.y;
  const len = Math.hypot(dx, dy) || 1;
  dx = dx / len; dy = dy / len;
  // 轴对齐：QFP/HDMI 焊盘多为水平或垂直出线
  if (Math.abs(dx) >= Math.abs(dy)) { dx = dx >= 0 ? 1 : -1; dy = 0; }
  else { dy = dy >= 0 ? 1 : -1; dx = 0; }
  for (const dist of [50, 70, 90, 110, 130]) {
    if (done) break;
    const x = Math.round(pad.x + dx * dist), y = Math.round(pad.y + dy * dist);
    let bad = false;
    for (const b of blockers) { if (Math.abs(b.x-x) < PAD_CLEAR && Math.abs(b.y-y) < PAD_CLEAR) { bad = true; break; } }
    if (!bad) for (const v of vias) { if (Math.abs(v.x-x) < VIA_CLEAR && Math.abs(v.y-y) < VIA_CLEAR) { bad = true; break; } }
    if (!bad) for (const b of blockers) { if (segmentHitsPad(pad.x, pad.y, x, y, b)) { bad = true; break; } }
    if (bad) continue;
    try {
      const v = await eda.pcb_PrimitiveVia.create(pad.net, x, y, HOLE, DIA);
      if (!v) continue;
      await eda.pcb_PrimitiveLine.create(pad.net, 1, pad.x, pad.y, x, y, TRACK_W, false);
      made += 1; fanout += 1; done = true;
      added.push({ x, y }); vias.push({ x, y });
    } catch (e) { /* 换下一个距离 */ }
  }
  if (!done) skipped += 1;
}

let rebuilt = 0;
for (const pour of (await eda.pcb_PrimitivePour.getAll()) || []) {
  try { await pour.rebuildCopperRegion(); rebuilt += 1; } catch (e) {}
}
await eda.pcb_Document.save();
return { candidates: own.length, made, inPad, fanout, skipped, rebuilt, vias: (await eda.pcb_PrimitiveVia.getAllPrimitiveId())?.length ?? null };
"""


def stitch(open_js: str, nets: list[str], hole: int, dia: int) -> None:
    code = (
        open_js
        + STITCH_JS.replace("__NETS__", json.dumps(nets, ensure_ascii=False))
        .replace("__HOLE__", str(hole))
        .replace("__DIA__", str(dia))
    )
    data, error = payload(code, 900)
    print(f"[stitch] {data if data else error}")


# ─── 器件外扩 ────────────────────────────────────────────────────────


def spread(open_js: str, k: float, edge: int) -> None:
    js = (
        open_js
        + f"const K = {k}, EDGE = {edge};"
        + """
let outline = null;
for (const id of (await eda.pcb_PrimitivePolyline.getAllPrimitiveId()) || []) {
  const got = await eda.pcb_PrimitivePolyline.get([id]);
  const one = Array.isArray(got) ? got[0] : got;
  if (one && one.getState_Layer() === 11) { outline = { id, poly: one.getState_Polygon() }; break; }
}
const pts = outline && outline.poly ? outline.poly.polygon : [];
const xs = [], ys = [];
for (let i = 0; i < pts.length - 1; i += 2) { if (typeof pts[i] === 'number') { xs.push(pts[i]); ys.push(pts[i+1]); } }
const x0 = Math.min(...xs), x1 = Math.max(...xs), y0 = Math.min(...ys), y1 = Math.max(...ys);
const cx = (x0+x1)/2, cy = (y0+y1)/2;
const nx0 = Math.round(cx + (x0-cx)*K) - 40, nx1 = Math.round(cx + (x1-cx)*K) + 40;
const ny0 = Math.round(cy + (y0-cy)*K) - 40, ny1 = Math.round(cy + (y1-cy)*K) + 40;
const comps = await eda.pcb_PrimitiveComponent.getAll();
const pads = await eda.pcb_PrimitivePad.getAll();
const box = new Map();
for (const p of pads) {
  const cid = String(p.getState_PrimitiveId()).slice(0, 16);
  const b = box.get(cid) || { minX: 1e9, maxX: -1e9, minY: 1e9, maxY: -1e9 };
  const x = p.getState_X(), y = p.getState_Y();
  b.minX = Math.min(b.minX, x); b.maxX = Math.max(b.maxX, x);
  b.minY = Math.min(b.minY, y); b.maxY = Math.max(b.maxY, y);
  box.set(cid, b);
}
let moved = 0;
for (const c of comps) {
  const b = box.get(c.getState_PrimitiveId());
  let nx = cx + (c.getState_X()-cx)*K, ny = cy + (c.getState_Y()-cy)*K;
  if (b) {
    const hw = (b.maxX-b.minX)/2, hh = (b.maxY-b.minY)/2;
    nx = Math.min(Math.max(nx, nx0+EDGE+hw), nx1-EDGE-hw);
    ny = Math.min(Math.max(ny, ny0+EDGE+hh), ny1-EDGE-hh);
  }
  await eda.pcb_PrimitiveComponent.modify(c.getState_PrimitiveId(), { x: nx, y: ny });
  moved += 1;
}
if (outline) await eda.pcb_PrimitivePolyline.delete([outline.id]);
await eda.pcb_PrimitivePolyline.create('', 11,
  eda.pcb_MathPolygon.createPolygon([nx0, ny0, 'L', nx0, ny1, nx1, ny1, nx1, ny0, nx0, ny0]), 10, false);
await eda.pcb_Document.clearRouting('all');
const pours = (await eda.pcb_PrimitivePour.getAll()) || [];
if (pours.length) await eda.pcb_PrimitivePour.delete(pours.map((p) => p.getState_PrimitiveId()));
await eda.pcb_Document.save();
return { moved, board: [nx0, ny0, nx1, ny1], mm: [Math.round((nx1-nx0)*0.0254), Math.round((ny1-ny0)*0.0254)] };
"""
    )
    data, error = payload(js, 300)
    print(f"[spread] {data if data else error}")


REBUILD_JS = """
const pours = (await eda.pcb_PrimitivePour.getAll()) || [];
let rebuilt = 0, failed = 0;
for (const p of pours) {
  try { await p.rebuildCopperRegion(); rebuilt += 1; } catch (e) { failed += 1; }
}
await eda.pcb_Document.save();
return { pours: pours.length, rebuilt, failed };
"""


def rebuild_pours(open_js: str) -> None:
    data, error = payload(open_js + REBUILD_JS, 600)
    print(f"[rebuild] {data if data else error}")


# ─── 位号丝印 ────────────────────────────────────────────────────────


SILK_JS = """
const APPLY = __APPLY__;
const FONT = 45, STROKE = 6, PAD_MARGIN = 10, TEXT_MARGIN = 8;
const comps = await eda.pcb_PrimitiveComponent.getAll();
const byId = new Map();
for (const c of comps) byId.set(c.getState_PrimitiveId(), c.getState_Designator() || '');
const pads = await eda.pcb_PrimitivePad.getAll();
const boxes = new Map();
for (const p of pads) {
  const cid = String(p.getState_PrimitiveId()).slice(0, 16);
  const b = boxes.get(cid) || { minX: 1e9, maxX: -1e9, minY: 1e9, maxY: -1e9 };
  const x = p.getState_X(), y = p.getState_Y();
  b.minX = Math.min(b.minX, x); b.maxX = Math.max(b.maxX, x);
  b.minY = Math.min(b.minY, y); b.maxY = Math.max(b.maxY, y);
  boxes.set(cid, b);
}
const padBoxes = [...boxes.entries()].map(([cid, b]) => ({ cid, ...b }));
const attrs = await eda.pcb_PrimitiveAttribute.getAll();
const targets = [];
for (const a of attrs) {
  if (a.getState_Key() !== 'Designator') continue;
  targets.push({ id: a.getState_PrimitiveId(), cid: a.getState_ParentPrimitiveId(),
    name: byId.get(a.getState_ParentPrimitiveId()) || '' });
}
function size(name, rotation, font) {
  const w0 = font * 0.62 * Math.max(1, name.length), h0 = font;
  return rotation % 180 === 90 ? { w: h0, h: w0 } : { w: w0, h: h0 };
}
const placed = [];
function hits(b, self) {
  for (const q of padBoxes) { if (q.cid === self) continue;
    if (b.minX < q.maxX+PAD_MARGIN && b.maxX > q.minX-PAD_MARGIN && b.minY < q.maxY+PAD_MARGIN && b.maxY > q.minY-PAD_MARGIN) return true; }
  for (const t of placed) {
    if (b.minX < t.maxX+TEXT_MARGIN && b.maxX > t.minX-TEXT_MARGIN && b.minY < t.maxY+TEXT_MARGIN && b.maxY > t.minY-TEXT_MARGIN) return true; }
  return false;
}
let overlapsPad = 0, overlapsText = 0, moved = 0, shrunk = 0, forced = 0;
const chosenList = [];
for (const t of targets) {
  const bx = boxes.get(t.cid); if (!bx) continue;
  const cx = (bx.minX+bx.maxX)/2, cy = (bx.minY+bx.maxY)/2;
  let chosen = null;
  if (APPLY) {
    for (const font of [45, 40, 36]) {
      for (const rotation of [0, 90]) {
        const { w, h } = size(t.name, rotation, font);
        const pad = 14;
        const cands = [
          { x: cx, y: bx.maxY+pad+h/2 }, { x: cx, y: bx.minY-pad-h/2 },
          { x: bx.maxX+pad+w/2, y: cy }, { x: bx.minX-pad-w/2, y: cy },
          { x: cx, y: bx.maxY+pad+h/2+26 }, { x: cx, y: bx.minY-pad-h/2-26 },
          { x: bx.maxX+pad+w/2+26, y: cy }, { x: bx.minX-pad-w/2-26, y: cy },
        ];
        for (const c of cands) {
          const b = { minX: c.x-w/2, maxX: c.x+w/2, minY: c.y-h/2, maxY: c.y+h/2 };
          if (!hits(b, t.cid)) { chosen = { ...c, rotation, font, b }; break; }
        }
        if (chosen) break;
      }
      if (chosen) break;
    }
    if (!chosen) { forced++; const { w, h } = size(t.name, 0, 36);
      const c = { x: cx, y: bx.maxY+14+h/2, rotation: 0, font: 36 };
      chosen = { ...c, b: { minX: c.x-w/2, maxX: c.x+w/2, minY: c.y-h/2, maxY: c.y+h/2 } }; }
    if (chosen.font < 45) shrunk++;
    placed.push(chosen.b);
    const attr = (await eda.pcb_PrimitiveAttribute.get([t.id]))[0].toAsync();
    attr.setState_X(chosen.x); attr.setState_Y(chosen.y);
    attr.setState_Rotation(chosen.rotation); attr.setState_FontSize(chosen.font);
    attr.setState_LineWidth(STROKE);
    await attr.done();
    moved++;
  } else {
    const a = (await eda.pcb_PrimitiveAttribute.get([t.id]))[0];
    const r = a.getState_Rotation() || 0, f = a.getState_FontSize() || FONT;
    const { w, h } = size(t.name, r, f);
    placed.push({ minX: a.getState_X()-w/2, maxX: a.getState_X()+w/2, minY: a.getState_Y()-h/2, maxY: a.getState_Y()+h/2 });
  }
}
for (let i = 0; i < placed.length; i++) {
  for (const q of padBoxes) {
    if (q.cid === targets[i]?.cid) continue;
    const b = placed[i];
    if (b.minX < q.maxX && b.maxX > q.minX && b.minY < q.maxY && b.maxY > q.minY) { overlapsPad++; break; }
  }
  for (let j = i+1; j < placed.length; j++) {
    const A = placed[i], B = placed[j];
    if (A.minX < B.maxX && A.maxX > B.minX && A.minY < B.maxY && A.maxY > B.minY) overlapsText++;
  }
}
if (APPLY) await eda.pcb_Document.save();
return { count: targets.length, overlapsPad, overlapsText, moved, shrunk, forced };
"""


def silk(open_js: str, apply: bool) -> None:
    code = open_js + SILK_JS.replace("__APPLY__", "true" if apply else "false")
    data, error = payload(code, 600)
    if data is None:
        print(f"[silk] 失败：{error[:110]}")
        return
    print(
        f"[silk] 位号 {data['count']} 个，压焊盘 {data['overlapsPad']}，互压 {data['overlapsText']}"
        + (f"，重排 {data['moved']}（缩字号 {data['shrunk']}，被迫 {data['forced']}）" if apply else "")
    )


# ─── 入口 ────────────────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(description="通用 PCB 收尾流水线：DRC 收敛 + 可装配性")
    parser.add_argument(
        "stage",
        choices=("status", "assemble", "spread", "route", "power", "stitch", "silk", "drc", "all"),
        default="status",
    )
    parser.add_argument("--pcb", default=None, help="PCB 文档 UUID（默认自动发现）")
    parser.add_argument("--power-nets", default=None, help="电源/地网络名，逗号分隔（默认自动识别）")
    parser.add_argument("--chunk", type=int, default=5, help="每片网络数")
    parser.add_argument("--layers", default=None, help="布线层，逗号分隔（默认 Top/Bottom）")
    parser.add_argument("--optimization", type=int, default=1, help="1=质量优先，0=快")
    parser.add_argument("--passes", type=int, default=3, help="route 重试轮数")
    parser.add_argument("--factor", type=float, default=1.45, help="spread 外扩系数")
    parser.add_argument("--edge", type=int, default=60, help="spread 后焊盘距板边")
    parser.add_argument("--hole", type=int, default=12, help="缝合孔孔径（mil）")
    parser.add_argument("--dia", type=int, default=24, help="缝合孔外径（mil）")
    parser.add_argument("--apply", action="store_true", help="silk：真正重排")
    parser.add_argument("--all-nets", action="store_true", help="route：布全部网络（默认只补未连通）")
    args = parser.parse_args()

    uuid = args.pcb or discover_pcb()
    if not uuid:
        return 2
    open_js = open_js_for(uuid)

    info = board_info(open_js)
    copper = [l["id"] for l in (info.get("copperLayers") or [])]
    nets = info.get("nets") or []
    power = (
        [n.strip() for n in args.power_nets.split(",") if n.strip()]
        if args.power_nets
        else [n for n in nets if re.match(r"^(GND|AGND|PGND|VCC|VDD|\\+?[0-9]+V[0-9]*$|.*VCC.*|.*VDD.*)", n, re.I)]
    )
    signal_layers = [i for i in copper if i in (1, 2)] or copper[:2]

    print(f"PCB={uuid}  铜层={copper}  网络={len(nets)}  电源/地候选={power}")

    if args.stage == "status":
        data = print_drc(open_js, "status")
        if data.get("unconnected"):
            print("  未连通网络：", json.dumps(data["unconnected"], ensure_ascii=False))
        return 0

    if args.stage == "assemble":
        assemble(open_js)
        return 0

    if args.stage == "spread":
        spread(open_js, args.factor, args.edge)
        assemble(open_js)
        return 0

    if args.stage == "drc":
        data = print_drc(open_js, "drc")
        return 0 if data.get("total") == 0 else 1

    if args.stage == "power":
        power_planes(open_js, power)
        print_drc(open_js, "power")
        return 0

    if args.stage == "stitch":
        stitch(open_js, power, args.hole, args.dia)
        print_drc(open_js, "stitch")
        return 0

    if args.stage == "silk":
        silk(open_js, args.apply)
        return 0

    if args.stage == "route":
        if args.all_nets:
            nets_to_route = nets
        else:
            data = print_drc(open_js, "before")
            # 信号优先：电源/地放在最后，避免布不通的电源组挡住信号
            nets_to_route = [n for n in (data.get("unconnected") or {}) if n and n != "unknown"]
        nets_to_route.sort(key=lambda n: (n in power, n))
        print(f"待布网络 {len(nets_to_route)} 个")
        route_nets(open_js, nets_to_route, args.chunk, signal_layers, args.optimization, args.passes)
        print_drc(open_js, "after")
        return 0

    # all：正确顺序 = 扇出(电源/地) → 布信号 → 平面 → 丝印 → 体检
    # 为什么扇出必须在布线之前：细间距焊盘的短扇出线要占用焊盘阵列外的通道，
    # 若先布信号，扇出线必然压到已有走线（实测 59 处 track-to-track）。
    print_drc(open_js, "start")
    data = print_drc(open_js, "scan")
    todo = [n for n in (data.get("unconnected") or {}) if n and n != "unknown"]
    signals = [n for n in todo if n not in power]

    print("\n[1/5] 电源/地扇出（先做，抢占焊盘阵列外通道）")
    stitch(open_js, power, args.hole, args.dia)
    print("\n[2/5] 电源/地平面")
    power_planes(open_js, power)
    print("\n[3/5] 布信号网络")
    if signals:
        route_nets(open_js, signals, args.chunk, signal_layers, args.optimization, args.passes)
    print("\n[4/5] 重建覆铜 + DRC（布线新增的过孔要重新开反焊盘）")
    rebuild_pours(open_js)
    print_drc(open_js, "after")
    print("\n[5/5] 位号丝印")
    silk(open_js, True)
    assemble(open_js)
    return 0


if __name__ == "__main__":
    sys.exit(main())
