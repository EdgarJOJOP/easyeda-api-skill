"""电路物理仿真（纯 Python，不依赖 numpy/scipy）。

覆盖：
* 电源树 DC 设定点（buck 分压 / LDO）
* buck 输出纹波
* PDN 阻抗（C + ESR + ESL 并联网络，1k~100MHz 扫频）
* 铜皮 IR drop（单段估算）
* 元件降额（电压/功率/电流 vs 额定值）

数据来源：从 EDA 导出的器件属性（Value / Voltage Rating / Footprint / LCSC Part Name）。
"""

from __future__ import annotations

import cmath
import math
from typing import Any

VREF = 0.6
VUSB = 5.0
FSW = 1.0e6
RHO_CU = 1.72e-8
OZ_M = 35e-6

# 电流预算（A）——与设计说明一致
CURRENTS = {"VCC5V": 1.0, "VCC3V3": 0.8, "VCC1V1": 0.5, "VCC2V5": 0.15}
RAIL_V = {"VCC5V": 5.0, "VCC3V3": 3.3, "VCC1V1": 1.1, "VCC2V5": 2.5, "BP_2V5": 2.5}

# 去耦分册（位号 → 轨）
RAILS: dict[str, list[str]] = {
    "VCC3V3": ["C2", "C8", "C9", "C12", "C27", "C28"],
    "VCC1V1": ["C3", "C16", "C17", "C18", "C19", "C20", "C22"],
    "VCC2V5": ["C5", "C24", "C25", "C26", "C29"],
    "VCC5V": ["C1", "C30"],
}

ESR = {"C0805": 3e-3, "C0402": 8e-3}
ESL = 0.8e-9

# 降额规则：limit 是允许的应力占比上限
DERATE = {"cap": 0.80, "res": 0.50, "ind": 0.70, "led": 0.50}

# 工作工况（从设计说明推导）
WORK_R = {  # 电阻：v 两端电压(V)、r 阻值(Ω)
    "R1": (3.3, 100e3), "R2": (0.6, 22e3), "R3": (1.1, 100e3), "R4": (0.6, 120e3),
    "R24": (1.3, 1e3), "R25": (3.0, 1e3),
    "R10": (3.3, 2.2e3), "R11": (3.3, 2.2e3), "R12": (3.3, 27e3),
    "R14": (3.3, 2.2e3), "R15": (3.3, 2.2e3), "R16": (3.3, 27e3), "R18": (3.3, 100e3),
}
WORK_L = {"L1": 0.8, "L2": 0.5}

DEFAULT_RATED = {
    "R0402": {"power": 0.0625}, "R0603": {"power": 0.1},
    "C0402": {"voltage": 16}, "C0603": {"voltage": 25}, "C0805": {"voltage": 25},
    "IND-SMD_L4.0-W4.0": {"isat": 2.0},
}


def parse_value(s: str) -> float:
    """把 '22uF' / '100nF' / '1kΩ' / '2.2uH' 解析成基本单位数值。"""
    if not s:
        return 0.0
    import re as _re
    t = _re.sub(r"[^0-9A-Za-z.]", "", str(s)).upper()  # 去掉 Ω/乱码/公差符号
    mult = 1.0
    for suf, m in (("PF", 1e-12), ("NF", 1e-9), ("UF", 1e-6), ("MF", 1e-3),
                   ("NH", 1e-9), ("UH", 1e-6), ("MH", 1e-3), ("H", 1.0),
                   ("K", 1e3), ("M", 1e6), ("", 1.0)):
        if t.endswith(suf) and (suf != "" or not t[-1].isalpha()):
            t = t[: -len(suf)] if suf else t
            mult = m
            break
    try:
        return float(t) * mult
    except ValueError:
        return 0.0


def vout_divider(r_top: float, r_bot: float) -> float:
    return VREF * (1 + r_top / r_bot)


def ripple(vin: float, vout: float, iout: float, l: float, c: float) -> float:
    d = vout / vin
    d_il = (vin - vout) * d / (l * FSW)
    return d_il / (8 * FSW * c)


def pdn_z(caps: list[tuple[float, str]], f: float) -> complex:
    """并联去耦网络在频率 f 的阻抗。"""
    admit = 0j
    for cval, pkg in caps:
        esr = ESR.get(pkg, 8e-3)
        z = esr + 1j * 2 * math.pi * f * ESL + 1 / (1j * 2 * math.pi * f * cval)
        admit += 1 / z
    return 1 / admit


def ir_drop(length_mil: float, width_mil: float, current: float, oz: float = 1.0) -> float:
    r = RHO_CU * (length_mil * 25.4e-6) / (width_mil * 25.4e-6 * oz * OZ_M)
    return r * current


def simulate(components: list[dict[str, Any]]) -> dict[str, Any]:
    """components: [{"des","value","voltageRating","footprint","lcsc"}]"""
    by_des: dict[str, dict] = {c["des"]: c for c in components}
    caps_val: dict[str, float] = {}
    for c in components:
        des = c["des"]
        if des.startswith("C"):
            v = parse_value(c.get("value", ""))
            if v > 0:
                caps_val[des] = v

    out: dict[str, Any] = {"rails": {}, "ripple": {}, "pdn": {}, "derating": {},
                           "irDrop": {}, "checks": []}

    # 1) 电源树
    r1 = parse_value(by_des.get("R1", {}).get("value", ""))
    r2 = parse_value(by_des.get("R2", {}).get("value", ""))
    r3 = parse_value(by_des.get("R3", {}).get("value", ""))
    r4 = parse_value(by_des.get("R4", {}).get("value", ""))
    if r1 and r2:
        out["rails"]["VCC3V3"] = vout_divider(r1, r2)
    if r3 and r4:
        out["rails"]["VCC1V1"] = vout_divider(r3, r4)
    out["rails"]["VCC2V5"] = 2.5
    nominal = {"VCC3V3": 3.3, "VCC1V1": 1.1, "VCC2V5": 2.5}
    for rail, sp in out["rails"].items():
        err = (sp - nominal[rail]) / nominal[rail] * 100
        out["checks"].append({"item": f"{rail} 设定点", "value": round(sp, 3),
                              "target": nominal[rail], "unit": "V", "pass": abs(err) <= 5})

    # 2) 纹波
    for rail, ldes, cdes in (("VCC3V3", "L1", "C2"), ("VCC1V1", "L2", "C3")):
        if rail not in out["rails"]:
            continue
        lval = parse_value(by_des.get(ldes, {}).get("value", ""))
        cval = caps_val.get(cdes, 0)
        if not lval or not cval:
            continue
        vin = VUSB if rail == "VCC3V3" else out["rails"]["VCC3V3"]
        dv = ripple(vin, out["rails"][rail], CURRENTS[rail], lval, cval)
        out["ripple"][rail] = dv
        out["checks"].append({"item": f"{rail} 输出纹波", "value": round(dv * 1e3, 2),
                              "target": 30.0, "unit": "mV", "pass": dv * 1e3 <= 30})

    # 3) PDN 阻抗
    for rail, des_list in RAILS.items():
        caps = [(caps_val[d], by_des.get(d, {}).get("footprint", "C0402")) for d in des_list if d in caps_val]
        if not caps:
            continue
        worst = 0.0
        at = 0.0
        f = 1e3
        while f <= 1e8:
            mag = abs(pdn_z(caps, f))
            if 1e5 <= f <= 1e7 and mag > worst:
                worst, at = mag, f
            f *= 1.05
        out["pdn"][rail] = {"worstOhm": round(worst, 4), "atHz": round(at)}
        out["checks"].append({"item": f"{rail} PDN 阻抗(100k-10M)", "value": round(worst, 4),
                              "target": 0.1, "unit": "Ω", "pass": worst <= 0.1})

    # 4) IR drop（按 500mil × 20mil 粗算）
    dv = ir_drop(500, 20, CURRENTS["VCC3V3"])
    out["irDrop"] = {"mV": round(dv * 1e3, 2), "pct": round(dv / 3.3 * 100, 2)}
    out["checks"].append({"item": "VCC3V3 IR drop(估)", "value": round(dv / 3.3 * 100, 2),
                          "target": 3.0, "unit": "%", "pass": dv / 3.3 * 100 <= 3})

    # 5) 降额
    for c in components:
        des = c["des"]
        fp = c.get("footprint", "")
        rv = c.get("voltageRating")
        if des.startswith("C"):
            rail = next((r for r, ls in RAILS.items() if des in ls), None)
            if rail is None:
                continue
            v_work = RAIL_V[rail]
            rated = parse_value(rv) if rv else DEFAULT_RATED.get(fp, {}).get("voltage", 16)
            if not rated:
                continue
            ratio = v_work / rated
            out["derating"][des] = {"stress": round(v_work, 2), "rated": round(rated, 2),
                                    "ratioPct": round(ratio * 100, 1), "unit": "V",
                                    "pass": ratio <= DERATE["cap"]}
        elif des in WORK_R:
            v_work, r = WORK_R[des]
            p = v_work * v_work / r
            rated = DEFAULT_RATED.get(fp, {}).get("power", 0.0625)
            ratio = p / rated
            out["derating"][des] = {"stress": round(p * 1e3, 2), "rated": round(rated * 1e3, 1),
                                    "ratioPct": round(ratio * 100, 1), "unit": "mW",
                                    "pass": ratio <= DERATE["res"]}
        elif des in WORK_L:
            i = WORK_L[des]
            rated = DEFAULT_RATED.get(fp, {}).get("isat", 2.0)
            ratio = i / rated
            out["derating"][des] = {"stress": i, "rated": rated,
                                    "ratioPct": round(ratio * 100, 1), "unit": "A",
                                    "pass": ratio <= DERATE["ind"]}
    der_fail = [d for d, v in out["derating"].items() if not v["pass"]]
    out["checks"].append({"item": "元件降额", "value": f"{len(out['derating']) - len(der_fail)}/{len(out['derating'])}",
                          "target": "全部", "unit": "", "pass": not der_fail})

    out["summary"] = {
        "total": len(out["checks"]),
        "passed": sum(1 for c in out["checks"] if c["pass"]),
        "failed": [c["item"] for c in out["checks"] if not c["pass"]],
    }
    return out


if __name__ == "__main__":  # pragma: no cover
    import json
    comps = json.load(open(r"D:\eda\deepseek\eda\diag\read_all_props.json", encoding="utf-8-sig"))["result"]
    items = [{"des": x["des"], "value": x["prop"].get("Value", ""),
              "voltageRating": x["prop"].get("Voltage Rating", ""),
              "footprint": x["prop"].get("Footprint", ""),
              "lcsc": x["prop"].get("LCSC Part Name", "")} for x in comps]
    print(json.dumps(simulate(items), ensure_ascii=False, indent=1))
