"""迷你 SPICE：MNA（Modified Nodal Analysis）求解器，纯 Python（无 numpy/scipy）。

支持：
* 线性元件：R / L / C / 独立电压源 V / 独立电流源 I / 受控源（E 压控压、G 压控流）
* 非线性：二极管 / LED（分段线性 + 牛顿迭代）
* 分析：DC 工作点、瞬态（后向欧拉）、AC 小信号扫频
* 输出：节点电压、电压源支路电流

用途：对电源树、去耦网络、LED 指示、上拉/分压做真实电路仿真，
而不是只算几个解析式。
"""

from __future__ import annotations

import cmath
import math
from typing import Any

KT_Q = 0.025852  # 热电压 @300K


def solve_linear(a: list[list[float]], b: list[float]) -> list[float] | None:
    """高斯消元（部分主元）。返回 None 表示奇异。"""
    n = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(m[r][col]))
        if abs(m[piv][col]) < 1e-15:
            return None
        if piv != col:
            m[col], m[piv] = m[piv], m[col]
        pv = m[col][col]
        for r in range(col + 1, n):
            f = m[r][col] / pv
            if f == 0:
                continue
            for c in range(col, n + 1):
                m[r][c] -= f * m[col][c]
    x = [0.0] * n
    for r in range(n - 1, -1, -1):
        s = m[r][n] - sum(m[r][c] * x[c] for c in range(r + 1, n))
        x[r] = s / m[r][r]
    return x


def solve_complex(a: list[list[complex]], b: list[complex]) -> list[complex] | None:
    n = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(m[r][col]))
        if abs(m[piv][col]) < 1e-15:
            return None
        if piv != col:
            m[col], m[piv] = m[piv], m[col]
        pv = m[col][col]
        for r in range(col + 1, n):
            f = m[r][col] / pv
            if f == 0:
                continue
            for c in range(col, n + 1):
                m[r][c] -= f * m[col][c]
    x = [0j] * n
    for r in range(n - 1, -1, -1):
        s = m[r][n] - sum(m[r][c] * x[c] for c in range(r + 1, n))
        x[r] = s / m[r][r]
    return x


class Circuit:
    """用节点名构建电路，内部自动编号。"""

    def __init__(self):
        self.nodes: dict[str, int] = {"0": 0}
        self.elements: list[dict[str, Any]] = []
        self._extra = 0

    def node(self, name: str) -> int:
        if name not in self.nodes:
            self._extra += 1
            self.nodes[name] = self._extra
        return self.nodes[name]

    # ── 元件 ──────────────────────────────────────────────
    def R(self, name, a, b, value):
        self.elements.append({"kind": "R", "name": name, "a": self.node(a), "b": self.node(b), "value": value})

    def L(self, name, a, b, value):
        self.elements.append({"kind": "L", "name": name, "a": self.node(a), "b": self.node(b), "value": value})

    def C(self, name, a, b, value, esr=0.0):
        self.elements.append({"kind": "C", "name": name, "a": self.node(a), "b": self.node(b),
                              "value": value, "esr": esr})

    def V(self, name, a, b, value):
        self.elements.append({"kind": "V", "name": name, "a": self.node(a), "b": self.node(b), "value": value})

    def I(self, name, a, b, value):
        self.elements.append({"kind": "I", "name": name, "a": self.node(a), "b": self.node(b), "value": value})

    def D(self, name, a, b, isat=1e-12, n=1.8):
        self.elements.append({"kind": "D", "name": name, "a": self.node(a), "b": self.node(b),
                              "isat": isat, "n": n})

    def E(self, name, a, b, c, d, gain):
        self.elements.append({"kind": "E", "name": name, "a": self.node(a), "b": self.node(b),
                              "c": self.node(c), "d": self.node(d), "gain": gain})

    # ── 分析 ──────────────────────────────────────────────
    def _dim(self):
        n = len(self.nodes)
        vs = sum(1 for e in self.elements if e["kind"] in ("V", "E", "L"))
        return n, vs

    def dc(self) -> dict[str, Any]:
        """DC 工作点：L 短路、C 开路、二极管牛顿迭代。"""
        return self._solve(newton=True, tran=None, omega=None)

    def tran(self, tstep: float, tstop: float, uic: dict[str, float] | None = None) -> dict[str, Any]:
        """瞬态（后向欧拉）。返回 {times, nodeWaveforms}。"""
        steps = max(1, int(round(tstop / tstep)))
        times = [i * tstep for i in range(steps + 1)]
        state: dict[str, list[float]] = {nm: [] for nm in self.nodes}
        # 初始状态（电容电压/电感电流）默认 0
        cap_v = {e["name"]: 0.0 for e in self.elements if e["kind"] == "C"}
        ind_i = {e["name"]: 0.0 for e in self.elements if e["kind"] == "L"}
        for k in range(steps + 1):
            t = times[k]
            sol = self._solve(newton=True, tran=(tstep, cap_v, ind_i), omega=None, t=t)
            if sol is None:
                return {"error": "singular", "t": t}
            for nm, idx in self.nodes.items():
                state[nm].append(sol["v"][idx])
            for e in self.elements:
                if e["kind"] == "C":
                    va = sol["v"][e["a"]]
                    vb = sol["v"][e["b"]]
                    cap_v[e["name"]] = va - vb
                elif e["kind"] == "L":
                    ind_i[e["name"]] = sol["i"][e["name"]]
        return {"times": times, "nodes": state}

    def ac(self, freqs: list[float]) -> dict[str, Any]:
        """逐频点做小信号 AC 分析。"""
        out: dict[str, Any] = {"freqs": freqs, "nodes": {nm: [] for nm in self.nodes}}
        for f in freqs:
            sol = self._solve(newton=False, tran=None, omega=2 * math.pi * f)
            if sol is None:
                return {"error": "singular", "f": f}
            for nm, idx in self.nodes.items():
                out["nodes"][nm].append(sol["v"][idx])
        return out


    def _solve(self, newton: bool, tran, omega, t=0.0):
        n, vs = self._dim()
        size = n + vs
        v_index = {}
        k = 0
        for e in self.elements:
            if e["kind"] in ("V", "E", "L"):
                v_index[e["name"]] = n + k
                k += 1

        v_guess = [0.0] * n
        for _ in range(60 if newton else 1):
            if omega is not None:
                a = [[0j] * size for _ in range(size)]
                b = [0j] * size
            else:
                a = [[0.0] * size for _ in range(size)]
                b = [0.0] * size

            def stamp_g(i, j, val):
                a[i][j] += val

            for e in self.elements:
                kind = e["kind"]
                if kind == "R":
                    g = 1.0 / max(e["value"], 1e-12)
                    stamp_g(e["a"], e["a"], g); stamp_g(e["b"], e["b"], g)
                    stamp_g(e["a"], e["b"], -g); stamp_g(e["b"], e["a"], -g)
                elif kind == "C":
                    if omega is not None:
                        y = 1j * omega * e["value"]
                        stamp_g(e["a"], e["a"], y); stamp_g(e["b"], e["b"], y)
                        stamp_g(e["a"], e["b"], -y); stamp_g(e["b"], e["a"], -y)
                    elif tran is not None:
                        dt = tran[0]
                        g = e["value"] / dt
                        ieq = g * tran[1].get(e["name"], 0.0)
                        stamp_g(e["a"], e["a"], g); stamp_g(e["b"], e["b"], g)
                        stamp_g(e["a"], e["b"], -g); stamp_g(e["b"], e["a"], -g)
                        b[e["a"]] += ieq
                        b[e["b"]] -= ieq
                    # DC：开路，不 stamp
                elif kind == "L":
                    if omega is not None:
                        y = 1.0 / (1j * omega * max(e["value"], 1e-15))
                        stamp_g(e["a"], e["a"], y); stamp_g(e["b"], e["b"], y)
                        stamp_g(e["a"], e["b"], -y); stamp_g(e["b"], e["a"], -y)
                    elif tran is not None:
                        dt = tran[0]
                        g = dt / max(e["value"], 1e-15)
                        stamp_g(e["a"], e["a"], g); stamp_g(e["b"], e["b"], g)
                        stamp_g(e["a"], e["b"], -g); stamp_g(e["b"], e["a"], -g)
                        b[e["a"]] -= tran[2].get(e["name"], 0.0)
                        b[e["b"]] += tran[2].get(e["name"], 0.0)
                    else:
                        # DC 短路：用 0V 源约束
                        idx = v_index[e["name"]]
                        stamp_g(e["a"], idx, 1.0); stamp_g(idx, e["a"], 1.0)
                        stamp_g(e["b"], idx, -1.0); stamp_g(idx, e["b"], -1.0)
                elif kind == "V":
                    idx = v_index[e["name"]]
                    stamp_g(e["a"], idx, 1.0); stamp_g(idx, e["a"], 1.0)
                    stamp_g(e["b"], idx, -1.0); stamp_g(idx, e["b"], -1.0)
                    # 1mΩ 内阻：避免多理想源/无阻尼网络导致矩阵奇异
                    stamp_g(idx, idx, 1e-3)
                    b[idx] += e["value"]
                elif kind == "I":
                    b[e["a"]] -= e["value"]
                    b[e["b"]] += e["value"]
                elif kind == "E":
                    idx = v_index[e["name"]]
                    stamp_g(e["a"], idx, 1.0); stamp_g(idx, e["a"], 1.0)
                    stamp_g(e["b"], idx, -1.0); stamp_g(idx, e["b"], -1.0)
                    stamp_g(idx, e["c"], -e["gain"]); stamp_g(idx, e["d"], e["gain"])
                elif kind == "D":
                    vd = v_guess[e["a"]] - v_guess[e["b"]]
                    vt = e["n"] * KT_Q
                    ex = math.exp(min(vd / vt, 40.0))
                    id_ = e["isat"] * (ex - 1.0)
                    gd = e["isat"] * ex / vt
                    ieq = id_ - gd * vd
                    stamp_g(e["a"], e["a"], gd); stamp_g(e["b"], e["b"], gd)
                    stamp_g(e["a"], e["b"], -gd); stamp_g(e["b"], e["a"], -gd)
                    b[e["a"]] -= ieq
                    b[e["b"]] += ieq

            # 接地节点行处理
            a[0] = [0.0] * size if omega is None else [0j] * size
            a[0][0] = 1.0
            b[0] = 0.0 if omega is None else 0j

            x = solve_linear(a, b) if omega is None else solve_complex(a, b)
            if x is None:
                return None
            newv = [x[i] for i in range(n)] if omega is not None else [float(x[i]) for i in range(n)]
            if newton:
                delta = max((abs(newv[i] - v_guess[i]) for i in range(n)), default=0.0)
                v_guess = newv
                if delta < 1e-6:
                    break
            else:
                v_guess = newv

        currents = {name: x[idx] for name, idx in v_index.items()} if omega is not None else {name: float(x[idx]) for name, idx in v_index.items()}
        if omega is not None:
            currents = {name: x[idx] for name, idx in v_index.items()}
        return {"v": [float(x[i]) if omega is None else x[i] for i in range(n)],
                "i": currents, "x": x}
