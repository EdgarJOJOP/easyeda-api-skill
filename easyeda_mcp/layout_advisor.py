"""通道与连通性分析：两套判定严格分离。

| 判定 | 实现 | 回答的问题 |
|---|---|---|
| ``net_connected`` | 精确几何并查集 | 这个网络电气上是否已经连通（删线后是否断开） |
| ``channel_reachable`` | 栅格 flood fill | 两个点之间有没有物理通道（移开器件能否腾出空间） |
| ``blockers`` | 栅格反事实 | 移开哪个器件能让"空间可达"从无变有 |
| ``channel_heatmap`` | 栅格占用率 | 哪块区域被铜/器件堵死 |

为什么必须分开：并查集判断的是"同网络图元是否接触"，异网络器件不会切断同网络走线，
所以它永远回答不了"器件挡路"；栅格判断的是"空间有没有空"，细走线/精确接触又会有量化误差，
所以它回答不了"删线后是否断开"。两者混用就会出现假连通或 blockers 失效。
"""

from __future__ import annotations

import math
from array import array
from collections import deque
from typing import Any

RES = 4
BLOCKER_RES = 8
TOL = 0.5
PLANE_LAYER = {15: 1, 16: 2}
SEG_BITS = 64


# ══════════════════════════════════════════════════════════════
# 一、精确几何（电气连通）
# ══════════════════════════════════════════════════════════════
def pt_seg(px, py, x1, y1, x2, y2):
    dx, dy = x2 - x1, y2 - y1
    l2 = dx * dx + dy * dy
    if l2 == 0:
        return math.hypot(px - x1, py - y1)
    t = max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / l2))
    return math.hypot(px - (x1 + t * dx), py - (y1 + t * dy))


def seg_seg(x1, y1, x2, y2, x3, y3, x4, y4):
    d1 = (x2 - x1) * (y3 - y1) - (y2 - y1) * (x3 - x1)
    d2 = (x2 - x1) * (y4 - y1) - (y2 - y1) * (x4 - x1)
    d3 = (x4 - x3) * (y1 - y3) - (y4 - y3) * (x1 - x3)
    d4 = (x4 - x3) * (y2 - y3) - (y4 - y3) * (x2 - x3)
    if ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0)):
        return 0.0
    return min(pt_seg(x1, y1, x3, y3, x4, y4), pt_seg(x2, y2, x3, y3, x4, y4),
               pt_seg(x3, y3, x1, y1, x2, y2), pt_seg(x4, y4, x1, y1, x2, y2))


def pt_pad(x, y, p):
    if p.get("shape") == "ELLIPSE":
        return max(0.0, math.hypot(x - p["x"], y - p["y"]) - max(p["w"], p["h"]) / 2)
    th = math.radians(p.get("rot") or 0)
    c, s = math.cos(th), math.sin(th)
    dx, dy = x - p["x"], y - p["y"]
    rx, ry = dx * c + dy * s, -dx * s + dy * c
    qx = max(abs(rx) - p["w"] / 2, 0.0)
    qy = max(abs(ry) - p["h"] / 2, 0.0)
    return math.hypot(qx, qy)


def seg_pad(x1, y1, x2, y2, p):
    if p.get("shape") == "ELLIPSE":
        return max(0.0, pt_seg(p["x"], p["y"], x1, y1, x2, y2) - max(p["w"], p["h"]) / 2)
    th = math.radians(p.get("rot") or 0)
    c, s = math.cos(th), math.sin(th)
    def tr(x, y):
        dx, dy = x - p["x"], y - p["y"]
        return dx * c + dy * s, -dx * s + dy * c
    ax, ay = tr(x1, y1)
    bx, by = tr(x2, y2)
    hw, hh = p["w"] / 2, p["h"] / 2
    if (abs(ax) <= hw and abs(ay) <= hh) or (abs(bx) <= hw and abs(by) <= hh):
        return 0.0
    edges = [(-hw, -hh, hw, -hh), (hw, -hh, hw, hh), (hw, hh, -hw, hh), (-hw, hh, -hw, -hh)]
    return min(seg_seg(ax, ay, bx, by, ex1, ey1, ex2, ey2) for (ex1, ey1, ex2, ey2) in edges)


def pad_pad(a, b):
    def samples(p):
        if p.get("shape") == "ELLIPSE":
            r = max(p["w"], p["h"]) / 2
            return [(p["x"] + r * math.cos(t), p["y"] + r * math.sin(t)) for t in
                    (0, math.pi / 2, math.pi, 3 * math.pi / 2)]
        th = math.radians(p.get("rot") or 0)
        c, s = math.cos(th), math.sin(th)
        hw, hh = p["w"] / 2, p["h"] / 2
        return [(p["x"] + dx * c - dy * s, p["y"] + dx * s + dy * c)
                for dx, dy in ((hw, hh), (-hw, hh), (-hw, -hh), (hw, -hh))]
    best = 1e9
    for (x, y) in samples(a):
        best = min(best, pt_pad(x, y, b))
    for (x, y) in samples(b):
        best = min(best, pt_pad(x, y, a))
    return best


class DSU:
    def __init__(self, n):
        self.p = list(range(n))

    def find(self, a):
        while self.p[a] != a:
            self.p[a] = self.p[self.p[a]]
            a = self.p[a]
        return a

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[ra] = rb


def net_connected(geom: dict[str, Any], net: str, ignore: set[str] | None = None) -> bool:
    """电气连通：该网络所有焊盘是否在同一并查集分量。"""
    ignore = ignore or set()
    pads = [p for p in geom.get("pads", []) if p.get("net") == net and p.get("des") not in ignore]
    if len(pads) < 2:
        return True
    lines = [l for l in geom.get("lines", []) if l.get("net") == net]
    vias = [v for v in geom.get("vias", []) if v.get("net") == net]
    items = [("pad", p) for p in pads] + [("line", l) for l in lines] + [("via", v) for v in vias]
    n = len(items)
    dsu = DSU(n)

    def near(i, j):
        ka, a = items[i]
        kb, b = items[j]
        if ka == "pad" and kb == "pad":
            return pad_pad(a, b) <= TOL
        if ka == "line" and kb == "line":
            la = 0 if a["layer"] == 1 else 3
            lb = 0 if b["layer"] == 1 else 3
            if la != lb:
                return False
            return seg_seg(a["x1"], a["y1"], a["x2"], a["y2"], b["x1"], b["y1"], b["x2"], b["y2"]) <= TOL
        if ka == "line" and kb == "pad":
            a, b = b, a
            ka, kb = kb, ka
        if ka == "pad" and kb == "line":
            if a.get("layer") not in (12, b["layer"]):
                return False
            return seg_pad(b["x1"], b["y1"], b["x2"], b["y2"], a) <= TOL
        if ka == "via" or kb == "via":
            v = a if ka == "via" else b
            o = b if ka == "via" else a
            r = (v.get("dia") or 24) / 2
            if "x1" in o:
                return max(0.0, pt_seg(v["x"], v["y"], o["x1"], o["y1"], o["x2"], o["y2"]) - r) <= TOL
            if "w" in o:
                return pt_pad(v["x"], v["y"], o) <= r + TOL
            return math.hypot(v["x"] - o["x"], v["y"] - o["y"]) <= r + (o.get("dia") or 24) / 2 + TOL
        return False

    for i in range(n):
        for j in range(i + 1, n):
            if near(i, j):
                dsu.union(i, j)
    pad_idx = [i for i, (k, _) in enumerate(items) if k == "pad"]
    root = dsu.find(pad_idx[0])
    return all(dsu.find(i) == root for i in pad_idx)


# ══════════════════════════════════════════════════════════════
# 二、栅格通道（空间可布）
# ══════════════════════════════════════════════════════════════
class Grid:
    def __init__(self, outline, res=RES, net_count=1):
        xs, ys = outline[0::2], outline[1::2]
        self.x0, self.x1 = min(xs), max(xs)
        self.y0, self.y1 = min(ys), max(ys)
        self.res = res
        self.nx = int((self.x1 - self.x0) / res) + 1
        self.ny = int((self.y1 - self.y0) / res) + 1
        self.seg_count = max(1, (net_count + SEG_BITS - 1) // SEG_BITS)
        self.cells = [[array("Q", [0]) * (self.nx * self.ny) for _ in range(self.seg_count)] for _ in range(4)]
        self.swap = set()
        self.net_ids = {}

    def gx(self, x):
        return int(round((x - self.x0) / self.res))

    def gy(self, y):
        return int(round((y - self.y0) / self.res))

    def _idx(self, gx, gy):
        if gx < 0 or gy < 0 or gx >= self.nx or gy >= self.ny:
            return None
        return gy * self.nx + gx

    def _stamp(self, gx, gy, net_id, layer):
        idx = self._idx(gx, gy)
        if idx is None:
            return
        seg, bit = divmod(net_id, SEG_BITS)
        arr = self.cells[layer][seg]
        arr[idx] = arr[idx] | (1 << bit)

    def blocked(self, gx, gy, net_id, layer):
        idx = self._idx(gx, gy)
        if idx is None:
            return True
        if not any(a[idx] for a in self.cells[layer]):
            return False
        seg, bit = divmod(net_id, SEG_BITS)
        return not (self.cells[layer][seg][idx] & (1 << bit))

    def _circle(self, layer, cx, cy, r, net_id):
        layers = [0, 1, 2, 3] if layer == 12 else {1: [0], 2: [3], 15: [1], 16: [2]}.get(layer, [0])
        gr = int(math.ceil(r / self.res))
        gx, gy = self.gx(cx), self.gy(cy)
        for L in layers:
            for yy in range(gy - gr, gy + gr + 1):
                for xx in range(gx - gr, gx + gr + 1):
                    if (xx - gx) ** 2 + (yy - gy) ** 2 <= (r / self.res) ** 2:
                        self._stamp(xx, yy, net_id, L)

    def _rect(self, layer, cx, cy, w, h, rot, net_id):
        layers = [0, 1, 2, 3] if layer == 12 else {1: [0], 2: [3], 15: [1], 16: [2]}.get(layer, [0])
        gr = int(math.ceil(math.hypot(w, h) / 2 / self.res))
        gx, gy = self.gx(cx), self.gy(cy)
        th = math.radians(rot or 0)
        c, s = math.cos(th), math.sin(th)
        for L in layers:
            for yy in range(gy - gr, gy + gr + 1):
                dy = (yy - gy) * self.res
                for xx in range(gx - gr, gx + gr + 1):
                    dx = (xx - gx) * self.res
                    rx, ry = dx * c + dy * s, -dx * s + dy * c
                    if abs(rx) <= w / 2 and abs(ry) <= h / 2:
                        self._stamp(xx, yy, net_id, L)

    def _seg(self, layer, x1, y1, x2, y2, w, net_id):
        L = 0 if layer == 1 else 3
        n = max(1, int(math.hypot(x2 - x1, y2 - y1) / self.res))
        for k in range(n + 1):
            t = k / n
            self._circle(L, x1 + (x2 - x1) * t, y1 + (y2 - y1) * t, max(w / 2, 1.0), net_id)


def build_grid(geom, ignore_comps=None, res=RES):
    """构建通道栅格；ignore_comps 里的器件焊盘不参与（反事实用）。"""
    ignore = ignore_comps or set()
    names = sorted({p.get("net") for p in geom.get("pads", []) if p.get("net")})
    g = Grid(geom["outline"], res, net_count=max(1, len(names)))
    g.net_ids = {n: i for i, n in enumerate(names)}
    edge_id = len(names)
    if edge_id >= g.seg_count * SEG_BITS:
        g.cells = [g.cells[L] + [array("Q", [0]) * (g.nx * g.ny)] for L in range(4)]
        g.seg_count += 1
    for L in range(4):
        for gx in range(g.nx):
            for gy in (0, g.ny - 1):
                g._stamp(gx, gy, edge_id, L)
        for gy in range(g.ny):
            for gx in (0, g.nx - 1):
                g._stamp(gx, gy, edge_id, L)
    for p in geom.get("pads", []):
        n = p.get("net") or ""
        if not n or p.get("des") in ignore:
            continue
        nid = g.net_ids[n]
        if p.get("shape") == "ELLIPSE":
            g._circle(p["layer"], p["x"], p["y"], max(p["w"], p["h"]) / 2, nid)
        else:
            g._rect(p["layer"], p["x"], p["y"], p["w"], p["h"], p.get("rot", 0), nid)
    for l in geom.get("lines", []):
        n = l.get("net") or ""
        if n:
            g._seg(l["layer"], l["x1"], l["y1"], l["x2"], l["y2"], l.get("width") or 8, g.net_ids[n])
    for v in geom.get("vias", []):
        n = v.get("net") or ""
        if n:
            g._circle(12, v["x"], v["y"], (v.get("dia") or 24) / 2, g.net_ids[n])
            g.swap.add((g.gx(v["x"]), g.gy(v["y"])))
    for p in geom.get("pads", []):
        if p.get("layer") == 12:
            g.swap.add((g.gx(p["x"]), g.gy(p["y"])))
    for pr in geom.get("pours", []):
        n = pr.get("net") or ""
        L = PLANE_LAYER.get(pr.get("layer"))
        nid = g.net_ids.get(n)
        if L is None or nid is None:
            continue
        seg, bit = divmod(nid, SEG_BITS)
        arr = g.cells[L][seg]
        for i in range(len(arr)):
            arr[i] = arr[i] | (1 << bit)
    return g


def channel_reachable(geom, net, ignore_comps=None, res=BLOCKER_RES):
    """空间可达：该网络焊盘之间是否存在通道（只走空格或同网络格）。"""
    ignore = ignore_comps or set()
    pads = [p for p in geom.get("pads", []) if p.get("net") == net and p.get("des") not in ignore]
    if len(pads) < 2:
        return True
    g = build_grid(geom, ignore, res)
    nid = g.net_ids[net]
    a = pads[0]
    goal = (g.gx(a["x"]), g.gy(a["y"]))
    starts = [0] if a.get("layer") != 12 else [0, 1, 2, 3]
    q = deque((g.gx(a["x"]), g.gy(a["y"]), L) for L in starts)
    seen = set(q)
    targets = {(g.gx(p["x"]), g.gy(p["y"])) for p in pads[1:]}
    while q:
        x, y, L = q.popleft()
        if (x, y) in targets:
            targets.discard((x, y))
            if not targets:
                return True
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (1, -1), (-1, 1), (-1, -1)):
            nx, ny = x + dx, y + dy
            if g.blocked(nx, ny, nid, L) or (nx, ny, L) in seen:
                continue
            seen.add((nx, ny, L))
            q.append((nx, ny, L))
        if (x, y) in g.swap:
            for nl in range(4):
                if nl == L or (x, y, nl) in seen or g.blocked(x, y, nid, nl):
                    continue
                seen.add((x, y, nl))
                q.append((x, y, nl))
    return not targets


def nearby_components(geom, net, radius=100.0):
    """按包围盒相交筛候选：网络焊盘 bbox 外扩 radius，与器件 bbox 相交即纳入。

    不能用"器件中心到焊盘距离"——J1 这种大连接器移到通道中点后中心离焊盘很远，
    但它的包围盒横跨通道，用中心距离会把它漏掉（真板实测 blockers 因此返回空）。
    """
    pads = [p for p in geom.get("pads", []) if p.get("net") == net]
    if not pads:
        return []
    nx0 = min(p["x"] - p["w"] / 2 for p in pads) - radius
    nx1 = max(p["x"] + p["w"] / 2 for p in pads) + radius
    ny0 = min(p["y"] - p["h"] / 2 for p in pads) - radius
    ny1 = max(p["y"] + p["h"] / 2 for p in pads) + radius
    boxes: dict[str, list[float]] = {}
    for p in geom.get("pads", []):
        des = p.get("des")
        if not des:
            continue
        b = boxes.setdefault(des, [1e9, -1e9, 1e9, -1e9])
        b[0] = min(b[0], p["x"] - p["w"] / 2)
        b[1] = max(b[1], p["x"] + p["w"] / 2)
        b[2] = min(b[2], p["y"] - p["h"] / 2)
        b[3] = max(b[3], p["y"] + p["h"] / 2)
    return [des for des, b in boxes.items() if b[0] <= nx1 and b[1] >= nx0 and b[2] <= ny1 and b[3] >= ny0]


def blockers(geom, net, candidates, max_candidates=0):
    """空间反事实：移开哪个器件后，该网络从"没有通道"变成"有通道"。"""
    pads = [p for p in geom.get("pads", []) if p.get("net") == net]
    if not pads:
        return []
    picked = list(candidates) if max_candidates <= 0 else list(candidates)[:max_candidates]
    if channel_reachable(geom, net):
        return []
    out = []
    for des in picked:
        if channel_reachable(geom, net, {des}):
            out.append(des)
    return out


def channel_heatmap(geom, gx_count=24, gy_count=16):
    g = build_grid(geom)
    rows = []
    for gy in range(gy_count):
        row = []
        ya = int(gy * g.ny / gy_count)
        yb = max(ya + 1, int((gy + 1) * g.ny / gy_count))
        for gx in range(gx_count):
            xa = int(gx * g.nx / gx_count)
            xb = max(xa + 1, int((gx + 1) * g.nx / gx_count))
            total = 0
            blocked = 0
            for L in (0, 3):
                for y in range(ya, yb):
                    base = y * g.nx
                    for x in range(xa, xb):
                        total += 1
                        if any(arr[base + x] for arr in g.cells[L]):
                            blocked += 1
            row.append(int(blocked * 100 / max(1, total)))
        rows.append(row)
    return rows
