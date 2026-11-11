"""PCB DRC 实时守护（兼容 EDA 4.2+ 原生实时 DRC 与 4.2 以下的降级轮询）。

背景
----
``pcb_Drc.startRealTimeDrc()`` / ``getRealTimeDrcStatus()`` 是 **EDA v4.2** 才引入的
API；4.2 以下调用恒返回 ``false``，原生实时 DRC 引擎不存在。``pcb_Drc.check()``
是同步全量检查（本板 3~8 秒），MCP 每次调用是独立请求，既无缓存也不感知变更。

双版本策略
----------
* **EDA >= 4.2**：``watch`` 时调用 ``startRealTimeDrc()``，并注册
  ``pcb_Event.addRealTimeDrcResultEventListener`` 把引擎推送的结果写进缓存 —— 真正的实时。
* **EDA < 4.2**：没有实时引擎，降级为"事件驱动 + 主动检查"：
  注册 ``addPrimitiveEventListener`` 记录变更；读取时**先作废旧结果再跑 check**
  （``clearBeforeCheck``），保证返回的一定是当前这一刻的 DRC，而不是上一次的缓存。

版本能力通过 ``eda.sys_Environment.getEditorCurrentVersion()`` /
``isEditorCurrentVersionGreaterThanOrEqual('4.2.0')`` 探测，并可用 ``mode``
参数强制走某条分支（``realtime`` / ``poll`` / ``auto``），便于在不同版本上做分支验证。

本模块只生成注入 EDA 的 JS、规范化返回值，不依赖 MCP SDK，方便逐分支单测。
"""

from __future__ import annotations

import json
from typing import Any

#: 支持的动作。
ACTIONS = ("status", "check", "watch", "unwatch", "diff", "capabilities")

#: 运行模式：auto 按版本选择，realtime 强制原生，poll 强制降级。
MODES = ("auto", "realtime", "poll")

#: 返回违规明细时的默认/最大条数。
DEFAULT_LIMIT = 50
MAX_LIMIT = 500

#: 缓存视为"新鲜"的默认毫秒数；超过则 status 会主动重跑。
DEFAULT_TTL_MS = 5000

_GUARD_JS = r"""
const G = globalThis;
const KEY = '__dsh_drc_guard';
const LID = 'dsh_drc_guard';
const RT_LID = 'dsh_drc_rt';
const OP = __OP__;
const OPTS = __OPTS__;

const st = G[KEY] || (G[KEY] = {
  version: 1,
  createdAt: Date.now(),
  registeredAt: null,
  lastChangeAt: null,
  changeCount: 0,
  dirty: false,
  running: false,
  auto: false,
  autoCount: 0,
  mode: null,
  caps: null,
  lastCheck: null,
  lastError: null,
  history: [],
});

function detectCaps() {
  if (st.caps && st.caps.detectedAt) return st.caps;
  const caps = { detectedAt: Date.now() };
  try { caps.version = eda.sys_Environment.getEditorCurrentVersion(false); } catch (e) { caps.versionError = String((e && e.message) || e); }
  try { caps.semanticVersion = eda.sys_Environment.getEditorCurrentVersion(true); } catch (e) { }
  try { caps.gte42 = eda.sys_Environment.isEditorCurrentVersionGreaterThanOrEqual('4.2.0'); } catch (e) { caps.gte42 = false; }
  caps.hasStart = typeof eda.pcb_Drc.startRealTimeDrc === 'function';
  caps.hasStop = typeof eda.pcb_Drc.stopRealTimeDrc === 'function';
  caps.hasRtEvent = typeof eda.pcb_Event.addRealTimeDrcResultEventListener === 'function';
  caps.hasPrimitiveEvent = typeof eda.pcb_Event.addPrimitiveEventListener === 'function';
  st.caps = caps;
  return caps;
}

function isRegistered(id) {
  try { return !!eda.pcb_Event.isEventListenerAlreadyExist(id); } catch (e) { return false; }
}

function flatten(raw) {
  const flat = [];
  for (const g of (raw || [])) {
    for (const sub of ((g && g.list) || [])) {
      for (const it of ((sub && sub.list) || [])) {
        flat.push({
          group: (g && g.name) || '',
          type: (sub && sub.name) || '',
          obj: (it && it.errorObjType) || '',
          pos: (it && it.pos) || null,
          obj1: (it && it.obj1 && it.obj1.suffix) || null,
          obj2: (it && it.obj2 && it.obj2.suffix) || null,
        });
      }
    }
  }
  return flat;
}

function summarize(flat) {
  const byGroup = {}, byType = {}, byObj = {};
  for (const f of flat) {
    byGroup[f.group] = (byGroup[f.group] || 0) + 1;
    byType[f.type] = (byType[f.type] || 0) + 1;
    byObj[f.obj] = (byObj[f.obj] || 0) + 1;
  }
  return {
    total: flat.length,
    clearance: byGroup['Clearance Error'] || 0,
    connection: byGroup['Connection Error'] || 0,
    netlist: byGroup['Netlist Error'] || 0,
    byGroup: byGroup,
    byType: byType,
    byObj: byObj,
  };
}

function keyOf(f) {
  const p = f.pos || {};
  const px = (typeof p.x === 'number') ? Math.round(p.x * 100) : 0;
  const py = (typeof p.y === 'number') ? Math.round(p.y * 100) : 0;
  return [f.group, f.type, f.obj1 || '', f.obj2 || '', px, py].join('|');
}

function storeSnapshot(flat, ms, src) {
  const prev = st.lastCheck && st.lastCheck.itemsFull ? st.lastCheck.itemsFull : [];
  const prevKeys = new Set(prev.map(keyOf));
  const curKeys = new Set(flat.map(keyOf));
  const added = flat.filter((f) => !prevKeys.has(keyOf(f)));
  const fixed = prev.filter((f) => !curKeys.has(keyOf(f)));
  const s = summarize(flat);
  st.lastCheck = {
    at: Date.now(), ms: ms, ok: true, source: src,
    total: s.total, clearance: s.clearance, connection: s.connection, netlist: s.netlist,
    byGroup: s.byGroup, byType: s.byType, byObj: s.byObj,
    added: added.length, fixed: fixed.length,
    addedItems: added.slice(0, OPTS.limit), itemsFull: flat,
  };
  st.dirty = false;
  st.lastError = null;
}

async function runCheck(force) {
  if (st.running) return { skipped: true };
  st.running = true;
  const started = Date.now();
  try {
    // 低版本没有实时引擎：先作废旧结果再检查（"删除旧 DRC → 重新检查"）
    if (OPTS.clearBeforeCheck) st.lastCheck = null;
    const raw = await eda.pcb_Drc.check(true, false, true);
    storeSnapshot(flatten(raw), Date.now() - started, 'manual');
    st.autoCount += 1;
    return { skipped: false };
  } catch (e) {
    st.lastError = 'check: ' + String((e && e.message) || e);
    return { skipped: false, error: st.lastError };
  } finally {
    st.running = false;
  }
}

function ingestRealtime(props) {
  const s = G[KEY]; if (!s) return;
  try {
    const raw = (props && props[0] && props[0].drcResult) ? props[0].drcResult : props;
    storeSnapshot(flatten(raw), 0, 'realtime');
  } catch (e) {
    s.lastError = 'realtime: ' + String((e && e.message) || e);
  }
}

function publicState(source, extra) {
  return Object.assign({
    ok: !st.lastError,
    source: source,
    version: st.version,
    mode: st.mode,
    caps: st.caps,
    auto: !!st.auto,
    autoCount: st.autoCount,
    registered: isRegistered(LID) || isRegistered(RT_LID),
    realtimeListener: isRegistered(RT_LID),
    changeCount: st.changeCount,
    lastChangeAt: st.lastChangeAt,
    ageMs: st.lastCheck ? (Date.now() - st.lastCheck.at) : null,
    dirty: !!st.dirty,
    history: st.history.slice(-10),
    lastError: st.lastError,
    cache: st.lastCheck ? {
      at: st.lastCheck.at, ms: st.lastCheck.ms, source: st.lastCheck.source,
      total: st.lastCheck.total, clearance: st.lastCheck.clearance,
      connection: st.lastCheck.connection, netlist: st.lastCheck.netlist,
      byGroup: st.lastCheck.byGroup, byType: st.lastCheck.byType, byObj: st.lastCheck.byObj,
      added: st.lastCheck.added, fixed: st.lastCheck.fixed,
    } : null,
  }, extra || {});
}

if (OP === 'capabilities') {
  detectCaps();
  return publicState('capabilities');
}

if (OP === 'watch') {
  st.auto = !!OPTS.auto;
  const caps = detectCaps();
  let modeUsed = 'poll';
  // 注意：isEditorCurrentVersionGreaterThanOrEqual('4.2.0') 在实测的 4.1.78 上会误报 true，
  // 因此不能拿版本号当门槛——只按 startRealTimeDrc() 的**实际返回值**判断能力。
  if (OPTS.mode === 'realtime' || (OPTS.mode === 'auto' && caps.hasStart && caps.hasRtEvent)) {
    let started = false;
    try { started = await eda.pcb_Drc.startRealTimeDrc(); } catch (e) { st.lastError = 'startRealTimeDrc: ' + String((e && e.message) || e); }
    if (started) {
      try {
        if (!isRegistered(RT_LID)) {
          eda.pcb_Event.addRealTimeDrcResultEventListener(RT_LID, 'all', function (eventType, props) { ingestRealtime(props); });
        }
        modeUsed = 'realtime';
      } catch (e) {
        st.lastError = 'rtListener: ' + String((e && e.message) || e);
      }
    }
  }
  if (modeUsed === 'poll' && caps.hasPrimitiveEvent && !isRegistered(LID)) {
    try {
      eda.pcb_Event.addPrimitiveEventListener(LID, 'all', function (eventType, props) {
        const s = G[KEY]; if (!s) return;
        s.changeCount += 1;
        s.lastChangeAt = Date.now();
        s.dirty = true;
        if (s.history.length < 40) s.history.push({ at: s.lastChangeAt, type: String(eventType), n: (props && props.length) || 0 });
        if (s.auto && !s.running) {
          if (s.autoTimer) clearTimeout(s.autoTimer);
          s.autoTimer = setTimeout(function () { runCheck(false); }, 1500);
        }
      }, false);
      st.registeredAt = Date.now();
    } catch (e) {
      st.lastError = 'register: ' + String((e && e.message) || e);
    }
  }
  st.mode = modeUsed;
  if (OPTS.runNow || !st.lastCheck) await runCheck(true);
  return publicState('watch', { modeUsed: modeUsed });
}

if (OP === 'unwatch') {
  try { eda.pcb_Event.removeEventListener(LID); } catch (e) { }
  try { eda.pcb_Event.removeEventListener(RT_LID); } catch (e) { }
  try { if (st.caps && st.caps.hasStop) await eda.pcb_Drc.stopRealTimeDrc(); } catch (e) { }
  st.auto = false;
  if (st.autoTimer) { clearTimeout(st.autoTimer); st.autoTimer = null; }
  return publicState('unwatch');
}

if (OP === 'check') {
  await runCheck(true);
  const extra = {};
  if (OPTS.includeItems && st.lastCheck) extra.items = st.lastCheck.itemsFull.slice(0, OPTS.limit);
  return publicState('fresh', extra);
}

if (OP === 'diff') {
  if (!st.lastCheck) { await runCheck(true); }
  const c = st.lastCheck;
  return publicState('diff', {
    diff: c ? { added: c.added, fixed: c.fixed, addedItems: c.addedItems } : null,
  });
}

// status：缓存新鲜就直接给，脏了/过期就重跑
const stale = !st.lastCheck || st.dirty || (Date.now() - st.lastCheck.at > OPTS.ttlMs);
if (stale || OPTS.force) { await runCheck(true); }
const extra2 = {};
if (OPTS.includeItems && st.lastCheck) extra2.items = st.lastCheck.itemsFull.slice(0, OPTS.limit);
return publicState(stale ? 'refreshed' : 'cache', extra2);
"""


def clamp_limit(limit: Any) -> int:
    """把 limit 收敛到 [1, MAX_LIMIT]；非法值回退默认值。"""
    if isinstance(limit, bool) or not isinstance(limit, (int, float)):
        return DEFAULT_LIMIT
    if limit != limit or limit in (float("inf"), float("-inf")):  # NaN / inf
        return DEFAULT_LIMIT
    value = int(limit)
    if value < 1:
        return 1
    return min(value, MAX_LIMIT)


def clamp_ttl(ttl_ms: Any) -> int:
    """把 TTL 收敛到 [0, 600000] 毫秒；非法值回退默认。"""
    if isinstance(ttl_ms, bool) or not isinstance(ttl_ms, (int, float)):
        return DEFAULT_TTL_MS
    if ttl_ms != ttl_ms:
        return DEFAULT_TTL_MS
    return max(0, min(int(ttl_ms), 600000))


def build_drc_js(
    action: str,
    *,
    include_items: bool = False,
    limit: int = DEFAULT_LIMIT,
    ttl_ms: int = DEFAULT_TTL_MS,
    auto: bool = False,
    force: bool = False,
    run_now: bool = False,
    mode: str = "auto",
    clear_before_check: bool = True,
) -> str:
    """生成注入 EDA 的 JS。``action``/``mode`` 非法时抛 ValueError。"""
    if action not in ACTIONS:
        raise ValueError(f"不支持的动作 {action!r}，可选：{', '.join(ACTIONS)}")
    if mode not in MODES:
        raise ValueError(f"不支持的 mode {mode!r}，可选：{', '.join(MODES)}")
    opts = {
        "includeItems": bool(include_items),
        "limit": clamp_limit(limit),
        "ttlMs": clamp_ttl(ttl_ms),
        "auto": bool(auto),
        "force": bool(force),
        "runNow": bool(run_now),
        "mode": mode,
        "clearBeforeCheck": bool(clear_before_check),
    }
    return (
        _GUARD_JS.replace("__OP__", json.dumps(action))
        .replace("__OPTS__", json.dumps(opts, ensure_ascii=False))
    )


def _as_int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    if value != value or value in (float("inf"), float("-inf")):
        return default
    return int(value)


def normalize_drc_result(raw: Any) -> dict[str, Any]:
    """把 EDA 返回值规范化；任何异常形状都返回可读的失败结构。"""
    if raw is None:
        return {"ok": False, "error": "EDA 没有返回结果（脚本可能没有 return）"}
    if not isinstance(raw, dict):
        return {"ok": False, "error": f"EDA 返回了非对象结果：{str(raw)[:200]}"}

    cache = raw.get("cache")
    if cache is not None and not isinstance(cache, dict):
        return {"ok": False, "error": "EDA 返回的 cache 结构非法"}

    def table(value: Any) -> dict[str, int]:
        if not isinstance(value, dict):
            return {}
        out: dict[str, int] = {}
        for k, v in value.items():
            out[str(k)] = _as_int(v)
        return out

    items = raw.get("items")
    if not isinstance(items, list):
        items = []

    caps = raw.get("caps")
    if not isinstance(caps, dict):
        caps = None

    result: dict[str, Any] = {
        "ok": bool(raw.get("ok", True)),
        "source": raw.get("source"),
        "mode": raw.get("mode"),
        "caps": caps,
        "auto": bool(raw.get("auto")),
        "autoCount": _as_int(raw.get("autoCount")),
        "registered": bool(raw.get("registered")),
        "realtimeListener": bool(raw.get("realtimeListener")),
        "changeCount": _as_int(raw.get("changeCount")),
        "lastChangeAt": raw.get("lastChangeAt"),
        "ageMs": raw.get("ageMs") if isinstance(raw.get("ageMs"), (int, float)) else None,
        "dirty": bool(raw.get("dirty")),
        "lastError": raw.get("lastError"),
        "history": raw.get("history") if isinstance(raw.get("history"), list) else [],
        "cache": None,
        "items": items,
    }
    if cache is not None:
        result["cache"] = {
            "at": cache.get("at"),
            "ms": _as_int(cache.get("ms")),
            "source": cache.get("source"),
            "total": _as_int(cache.get("total")),
            "clearance": _as_int(cache.get("clearance")),
            "connection": _as_int(cache.get("connection")),
            "netlist": _as_int(cache.get("netlist")),
            "byGroup": table(cache.get("byGroup")),
            "byType": table(cache.get("byType")),
            "byObj": table(cache.get("byObj")),
            "added": _as_int(cache.get("added")),
            "fixed": _as_int(cache.get("fixed")),
        }
    diff = raw.get("diff")
    if isinstance(diff, dict):
        result["diff"] = {
            "added": _as_int(diff.get("added")),
            "fixed": _as_int(diff.get("fixed")),
            "addedItems": diff.get("addedItems") if isinstance(diff.get("addedItems"), list) else [],
        }
    return result
