"""从 EDA 焊盘/网络数据自动构建 SPICE 电路，跑 DC/瞬态/AC。

建模策略（MS2133 无公开 SPICE 模型）：
* R/L/C：按值建模；
* 二极管（D7/D8 LED）：按 LED 模型；
* 电源网络：VCC5V 用 5V 理想源；VCC3V3/VCC1V1/VCC2V5 用理想电压源（buck/LDO 输出）；
* IC 的电源焊盘：用电流源按预算分摊（模拟负载）；
* 其它 IC 引脚：开路（不建模内部）。
"""

from __future__ import annotations

import math
from typing import Any

from .mna import Circuit

RAIL_V = {"VCC5V": 5.0, "VCC3V3": 3.3, "VCC1V1": 1.1, "VCC2V5": 2.5}
CURRENTS = {"VCC5V": 1.0, "VCC3V3": 0.8, "VCC1V1": 0.5, "VCC2V5": 0.15}


def parse_value(s: str) -> float:
    import re
    t = re.sub(r"[^0-9A-Za-z.]", "", str(s or "")).upper()
    mult = 1.0
    for suf, m in (("PF", 1e-12), ("NF", 1e-9), ("UF", 1e-6),
                   ("NH", 1e-9), ("UH", 1e-6), ("K", 1e3), ("M", 1e6)):
        if t.endswith(suf):
            t, mult = t[: -len(suf)], m
            break
    try:
        return float(t) * mult
    except ValueError:
        return 0.0


def pads_by_component(pads: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for p in pads:
        des = p.get("des") or ""
        if not des or not p.get("net"):
            continue
        out.setdefault(des, []).append(p)
    return out


def build_circuit(pads: list[dict], comps: list[dict], load_currents=None) -> tuple[Circuit, dict]:
    """返回 (Circuit, meta)。meta 记录节点/元件/负载。"""
    c = Circuit()
    by_comp = pads_by_component(pads)
    meta = {"elements": 0, "loads": [], "rails": []}

    # 1) 电源节点
    for rail, v in RAIL_V.items():
        c.node(rail)
        c.V(f"V_{rail}", rail, "0", v)
        meta["rails"].append(rail)

    # 2) 被动器件
    for des, plist in by_comp.items():
        if len(plist) < 2:
            continue
        prop = next((x for x in comps if x["des"] == des), {}).get("prop", {})
        value = parse_value(prop.get("Value", ""))
        a, b = plist[0]["net"], plist[1]["net"]
        if des.startswith("R") and value > 0:
            c.R(des, a, b, value)
            meta["elements"] += 1
        elif des.startswith("C") and value > 0:
            c.C(des, a, b, value, esr=3e-3)
            meta["elements"] += 1
        elif des.startswith("L") and value > 0:
            c.L(des, a, b, value)
            meta["elements"] += 1

    # 3) LED（D7/D8 接 VCC3V3/VCC5V 经 R24/R25 到 GND）
    for des in ("D7", "D8"):
        plist = by_comp.get(des, [])
        if len(plist) >= 2:
            c.D(des, plist[0]["net"], plist[1]["net"], isat=1e-14, n=2.0)
            meta["elements"] += 1

    # 4) 浮空节点加 1GΩ 泄漏电阻，避免 DC 矩阵奇异（SW_3V3/FB 等中间节点）
    for node in list(c.nodes):
        if node not in RAIL_V and node != "0":
            c.R(f"RLEAK_{node}", node, "0", 1e6)

    # 5) 负载：从对应轨流向地（电流源 rail -> 0）
    if load_currents is None:
        load_currents = CURRENTS
    for rail, i_total in load_currents.items():
        c.I(f"LOAD_{rail}", rail, "0", i_total)
        meta["loads"].append({"rail": rail, "current": i_total})

    return c, meta


def run_full(pads: list[dict], comps: list[dict], tstep=1e-7, tstop=2e-4) -> dict[str, Any]:
    c, meta = build_circuit(pads, comps)
    dc = c.dc()
    out: dict[str, Any] = {"meta": meta}
    if dc is None:
        return {"error": "DC 不收敛", "meta": meta}
    out["dc"] = {nm: round(dc["v"][c.nodes[nm]], 4) for nm in ("VCC5V", "VCC3V3", "VCC1V1", "VCC2V5") if nm in c.nodes}

    # 瞬态：5V 阶跃（用 DC 初值即可，重点看去耦节点）
    try:
        tr = c.tran(tstep, tstop)
        if "error" not in tr:
            node = "VCC3V3" if "VCC3V3" in c.nodes else list(c.nodes)[1]
            idx = c.nodes[node]
            wave = tr["nodes"][node]
            out["tran"] = {
                "node": node, "t0": round(wave[0], 4), "tEnd": round(wave[-1], 4),
                "min": round(min(wave), 4), "max": round(max(wave), 4),
                "samples": [round(w, 4) for w in wave[:: max(1, len(wave) // 10)]],
            }
    except Exception as exc:  # noqa: BLE001
        out["tranError"] = str(exc)

    # 逐轨 AC 阻抗 + 上电瞬态（单轨小电路，避免全板浮空节点）
    caps = rail_caps(pads, comps)
    out["rails"] = {}
    freqs = [1e2, 1e3, 1e4, 1e5, 1e6, 1e7]
    for rail, lst in caps.items():
        entry: dict[str, Any] = {"caps": [d for d, _ in lst]}
        ac = rail_ac(rail, lst, freqs)
        if "error" not in ac:
            entry["zOhm"] = ac["zOhm"]
        else:
            entry["acError"] = ac["error"]
        tr = rail_tran(rail, lst, CURRENTS[rail], tstep, tstop)
        if "error" not in tr:
            entry["tran"] = {k: tr[k] for k in ("t0", "tEnd", "min", "max", "t90_us")}
        else:
            entry["tranError"] = tr["error"]
        out["rails"][rail] = entry
    return out


def rail_ac(rail: str, caps: list[tuple[str, float]], freqs: list[float]) -> dict:
    """单轨 PDN 交流阻抗：电压源短路，注入 1A，测节点电压 = |Z|。"""
    c = Circuit()
    c.node(rail)
    for des, val in caps:
        if val > 0:
            c.C(des, rail, "0", val, esr=3e-3)
    c.I("IAC", "0", rail, 1.0)
    res = c.ac(freqs)
    if "error" in res:
        return {"error": res["error"]}
    return {"freqs": freqs, "zOhm": [round(abs(v), 6) for v in res["nodes"][rail]]}


def rail_tran(rail: str, caps: list[tuple[str, float]], load_a: float,
              tstep: float = 1e-7, tstop: float = 5e-4) -> dict:
    """单轨上电瞬态：5V/3.3V 阶跃经等效源阻抗给去耦网络充电。"""
    c = Circuit()
    c.node(rail)
    c.V("V1", "src", "0", RAIL_V[rail])
    c.R("Rsrc", "src", rail, 0.05)     # 等效源内阻 50mΩ
    for des, val in caps:
        if val > 0:
            c.C(des, rail, "0", val, esr=3e-3)
    if load_a > 0:
        c.I("ILOAD", rail, "0", load_a)
    tr = c.tran(tstep, tstop)
    if "error" in tr:
        return {"error": tr["error"]}
    w = tr["nodes"][rail]
    return {"rail": rail, "t0": round(w[0], 4), "tEnd": round(w[-1], 4),
            "min": round(min(w), 4), "max": round(max(w), 4),
            "t90_us": round(next((t * 1e6 for t, v in zip(tr["times"], w)
                                  if v >= 0.9 * RAIL_V[rail]), -1), 2)}


def rail_caps(pads: list[dict], comps: list[dict]) -> dict[str, list[tuple[str, float]]]:
    """按网络归集去耦电容（轨 -> [(des, F)])。"""
    by_comp = pads_by_component(pads)
    out: dict[str, list[tuple[str, float]]] = {r: [] for r in RAIL_V}
    for des, plist in by_comp.items():
        if not des.startswith("C") or len(plist) < 2:
            continue
        prop = next((x for x in comps if x["des"] == des), {}).get("prop", {})
        val = parse_value(prop.get("Value", ""))
        if val <= 0:
            continue
        nets = {plist[0]["net"], plist[1]["net"]}
        for rail in RAIL_V:
            if rail in nets and "GND" in nets:
                out[rail].append((des, val))
                break
    return out
