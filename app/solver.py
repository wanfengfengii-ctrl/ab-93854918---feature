"""晶圆栅格复原核心算法。

联合选择原点 ``O``、行基向量 ``A``、列基向量 ``B`` 以及标记到互异格位的分配，
依次最小化：

1. 弃点数（最多 ``max_outliers`` 个）；
2. 最大曼哈顿残差；
3. 曼哈顿残差总和；
4. 完整参数与按编号排列的分配序列（字典序）。

约束：``det(A, B) > 0``；被采用点的预测坐标逐分量不越过容差（L∞ ≤ tolerance）；
任意两个标记不得占用同一格位。

枚举规模有界：六个分量各取自跨度 ≤ 6 的闭区间，至多 ``7**6`` 组参数，
det 过滤、包围盒与邻域集合预过滤后，仅对候选参数运行二分图匹配
（最小费用最大流， successive shortest path）。
"""

from collections import deque
from itertools import product
from typing import Optional


# ---------------------------------------------------------------------------
# 最小费用最大流（图规模 ≤ ~70 节点 / ~700 边，SPFA 足够）
# ---------------------------------------------------------------------------
class _MinCostMaxFlow:
    def __init__(self, n: int):
        self.n = n
        self.g = [[] for _ in range(n)]

    def add_edge(self, u, v, cap, cost):
        self.g[u].append([v, cap, cost, len(self.g[v])])
        self.g[v].append([u, 0, -cost, len(self.g[u]) - 1])

    def run(self, s, t, need):
        """返回 (实际流量, 最小费用)，最多发送 need 个单位。"""
        n = self.n
        flow = 0
        cost = 0
        inf = 10**18
        while flow < need:
            dist = [inf] * n
            pv = [-1] * n
            pe = [-1] * n
            inq = [False] * n
            dist[s] = 0
            q = deque([s])
            inq[s] = True
            while q:
                u = q.popleft()
                inq[u] = False
                for ei, (v, cap, w, _) in enumerate(self.g[u]):
                    if cap > 0 and dist[u] + w < dist[v]:
                        dist[v] = dist[u] + w
                        pv[v] = u
                        pe[v] = ei
                        if not inq[v]:
                            q.append(v)
                            inq[v] = True
            if dist[t] == inf:
                break
            aug = need - flow
            v = t
            while v != s:
                u = pv[v]
                aug = min(aug, self.g[u][pe[v]][1])
                v = u
            v = t
            while v != s:
                u = pv[v]
                edge = self.g[u][pe[v]]
                edge[1] -= aug
                self.g[v][edge[3]][1] += aug
                cost += aug * edge[2]
                v = u
            flow += aug
        return flow, cost


def _augment(adj, p, match_cell, seen):
    """Kuhn 增广路 DFS。adj[p] 为可用格位序号列表。"""
    for c in adj[p]:
        if seen[c]:
            continue
        seen[c] = True
        if match_cell[c] < 0 or _augment(adj, match_cell[c], match_cell, seen):
            match_cell[c] = p
            return True
    return False


def _max_match(point_edges, n_points, n_cells, cap=None):
    adj = [
        [c for c, mh in point_edges[p] if cap is None or mh <= cap]
        for p in range(n_points)
    ]
    match_cell = [-1] * n_cells
    count = 0
    for p in range(n_points):
        seen = [False] * n_cells
        if _augment(adj, p, match_cell, seen):
            count += 1
    return count


def _min_cost_flow(point_edges, n_points, n_cells, need, cap):
    s = n_points + n_cells
    t = s + 1
    net = _MinCostMaxFlow(t + 1)
    for p in range(n_points):
        net.add_edge(s, p, 1, 0)
    for c in range(n_cells):
        net.add_edge(n_points + c, t, 1, 0)
    for p, edges in enumerate(point_edges):
        for c, mh in edges:
            if mh <= cap:
                net.add_edge(p, n_points + c, 1, mh)
    return net.run(s, t, need)


# ---------------------------------------------------------------------------
# 主求解流程
# ---------------------------------------------------------------------------
def reconstruct(points, rows, cols, tolerance, max_outliers, bounds):
    """复原栅格。

    points: [(id, x, y), ...]（调用方保证 7~14 个、编号唯一）。
    bounds: dict(origin=([lox,hix],[loy,hiy]), row_vector=..., col_vector=...)
    返回 dict；无解时返回 {"solvable": False, "reason": ...}。
    """
    n = len(points)
    # 字典序决胜定义在“按编号排列的分配序列”上，内部统一按编号排序
    points = sorted(points, key=lambda t: t[0])
    (oxr, oyr), (axr, ayr), (bxr, byr) = (
        bounds["origin"],
        bounds["row_vector"],
        bounds["col_vector"],
    )

    best = None  # (k, M, S)，参数与分配按枚举顺序天然保证字典序最小

    # 容差较小时用邻域集合做快速计数；否则直接用边表
    neighbor_offsets = None
    if tolerance >= 0 and (2 * tolerance + 1) ** 2 <= 64:
        neighbor_offsets = [
            (dx, dy)
            for dx in range(-tolerance, tolerance + 1)
            for dy in range(-tolerance, tolerance + 1)
        ]

    for ox, oy, ax, ay, bx, by in product(
        range(oxr[0], oxr[1] + 1),
        range(oyr[0], oyr[1] + 1),
        range(axr[0], axr[1] + 1),
        range(ayr[0], ayr[1] + 1),
        range(bxr[0], bxr[1] + 1),
        range(byr[0], byr[1] + 1),
    ):
        det = ax * by - ay * bx
        if det <= 0:
            continue

        # 预测格位（行主序，cell index = r*cols + c）
        cells = []
        minx = miny = 10**18
        maxx = maxy = -(10**18)
        for r in range(rows):
            for c in range(cols):
                x = ox + r * ax + c * bx
                y = oy + r * ay + c * by
                cells.append((x, y))
                if x < minx:
                    minx = x
                if x > maxx:
                    maxx = x
                if y < miny:
                    miny = y
                if y > maxy:
                    maxy = y

        # 第一级预过滤：包围盒（含容差扩张）
        bbox_hits = 0
        for _, px, py in points:
            if minx - tolerance <= px <= maxx + tolerance and (
                miny - tolerance <= py <= maxy + tolerance
            ):
                bbox_hits += 1
        if best is not None and bbox_hits < n - best[0][0]:
            continue

        # 第二级预过滤：每个标记是否能在容差内落到某个格位
        if neighbor_offsets is not None:
            cell_set = set(cells)
            near = 0
            for _, px, py in points:
                for dx, dy in neighbor_offsets:
                    if (px + dx, py + dy) in cell_set:
                        near += 1
                        break
        else:
            near = 0
            for _, px, py in points:
                for cx, cy in cells:
                    if abs(px - cx) <= tolerance and abs(py - cy) <= tolerance:
                        near += 1
                        break
        if best is not None and near < n - best[0][0]:
            continue

        # 构造边表：边仅在 L∞ <= tolerance 时存在
        edges = [[] for _ in range(n)]
        for p, (_, px, py) in enumerate(points):
            pe = []
            for ci, (cx, cy) in enumerate(cells):
                dx = px - cx
                dy = py - cy
                if abs(dx) <= tolerance and abs(dy) <= tolerance:
                    pe.append((ci, abs(dx) + abs(dy)))
            edges[p] = pe

        # 阶段一：最大化采用数（最小化弃点数）
        adopted = _max_match(edges, n, rows * cols)
        k = n - adopted
        if k > max_outliers:
            continue

        # 阶段二：在匹配数不变前提下最小化最大曼哈顿残差（二分阈值）
        mhs = sorted({mh for pe in edges for _, mh in pe})
        lo, hi = 0, len(mhs) - 1
        while lo < hi:
            mid = (lo + hi) // 2
            if _max_match(edges, n, rows * cols, cap=mhs[mid]) == adopted:
                hi = mid
            else:
                lo = mid + 1
        big_m = mhs[lo]

        # 阶段三：最小化残差总和
        flow, total_s = _min_cost_flow(edges, n, rows * cols, adopted, big_m)
        if flow != adopted:  # 理论上不会发生，防御性检查
            continue
        assert flow == adopted

        if best is not None and (k, big_m, total_s) >= best[0]:
            continue

        # 阶段四：该参数下字典序最小的分配（按编号顺序逐点贪心，
        # 每步用后缀最小费用流验证可行性）
        assignment = _lexicographic_assignment(
            edges, n, rows * cols, adopted, k, big_m, total_s
        )
        if assignment is None:
            continue

        best = (
            (k, big_m, total_s),
            (ox, oy, ax, ay, bx, by),
            det,
            cells,
            assignment,
            edges,
        )
        # (0, 0, 0) 为字典序最小的可能目标值，后续参数不可能更优
        if best[0] == (0, 0, 0):
            break

    if best is None:
        need = n - max_outliers
        return {
            "solvable": False,
            "reason": (
                f"在给定闭区间内不存在行列式为正的整数原点/基向量，能在容差 "
                f"{tolerance} 内为至少 {need} 个标记分配互异格位"
                f"（共 {n} 个标记，允许弃点 {max_outliers} 个、栅格 "
                f"{rows} 行 {cols} 列）；请放宽容差或参数区间，或提高弃点上限。"
            ),
        }

    (k, big_m, total_s), (ox, oy, ax, ay, bx, by), det, cells, assignment, edges = best
    return _build_response(
        points,
        rows,
        cols,
        tolerance,
        (ox, oy),
        (ax, ay),
        (bx, by),
        det,
        cells,
        assignment,
        k,
        big_m,
        total_s,
    )


def _lexicographic_assignment(edges, n, n_cells, adopted, k, big_m, total_s):
    """逐点（编号顺序）贪心：先试弃点（-1），再按格位序号试分配。

    每一步通过后缀最小费用流验证：剩余点能否在未占用格位上补足流量，
    且瓶颈 ≤ big_m、总费用恰好等于剩余预算。
    """
    assign = {}  # point -> cell
    discarded = set()
    spent = 0

    def suffix_feasible(frm, used_cells, need_flow, budget):
        """点 frm..n-1 是否能送出 need_flow 单位、费用恰为 budget。"""
        suffix_points = list(range(frm, n))
        if len(suffix_points) < need_flow:
            return False
        pidx = {p: i for i, p in enumerate(suffix_points)}
        ns = len(suffix_points)
        s = ns + n_cells
        t = s + 1
        net = _MinCostMaxFlow(t + 1)
        for p in suffix_points:
            net.add_edge(s, pidx[p], 1, 0)
        for c in range(n_cells):
            if c not in used_cells:
                net.add_edge(ns + c, t, 1, 0)
        for p in suffix_points:
            for c, mh in edges[p]:
                if c not in used_cells and mh <= big_m:
                    net.add_edge(pidx[p], ns + c, 1, mh)
        flow, cost = net.run(s, t, need_flow)
        return flow == need_flow and cost == budget

    for p in range(n):
        chosen = None
        # 选项 0：弃点（编码 -1，字典序中最先）
        if len(discarded) < k:
            disc = set(discarded)
            disc.add(p)
            need = adopted - len(assign)
            later = n - p - 1
            if (
                later >= need
                and suffix_feasible(p + 1, set(assign.values()), need, total_s - spent)
            ):
                chosen = -1
        # 选项 1..：按格位行主序分配
        if chosen is None:
            for c, mh in sorted(edges[p]):
                if c in assign.values() or mh > big_m:
                    continue
                used = set(assign.values())
                used.add(c)
                need = adopted - len(assign) - 1
                if suffix_feasible(
                    p + 1, used, need, total_s - spent - mh
                ):
                    chosen = c
                    break
        if chosen is None:
            return None
        if chosen == -1:
            discarded.add(p)
        else:
            assign[p] = chosen
            spent += dict(edges[p])[chosen]

    if len(assign) != adopted or len(discarded) != k:
        return None
    return {p: assign.get(p, -1) for p in range(n)}


def _build_response(
    points,
    rows,
    cols,
    tolerance,
    origin,
    row_vec,
    col_vec,
    det,
    cells,
    assignment,
    k,
    big_m,
    total_s,
):
    assignments = []
    discarded = []
    used_cells = set()
    for p, (pid, px, py) in enumerate(points):
        ci = assignment[p]
        if ci == -1:
            # 弃点证据：最近格位、最近曼哈顿距离、容差内候选格位
            best_c = -1
            best_mh = 10**18
            best_inf = 10**18
            within = []
            for cj, (cx, cy) in enumerate(cells):
                dx = px - cx
                dy = py - cy
                mh = abs(dx) + abs(dy)
                inf = max(abs(dx), abs(dy))
                if inf < best_inf or (inf == best_inf and mh < best_mh):
                    best_c = cj
                    best_mh = mh
                    best_inf = inf
                if inf <= tolerance:
                    within.append([cj // cols, cj % cols])
            evidence = {
                "id": pid,
                "x": px,
                "y": py,
                "nearest_cell": [best_c // cols, best_c % cols],
                "nearest_manhattan_residual": best_mh,
                "nearest_inf_residual": best_inf,
                "cells_within_tolerance": within,
                "reason": (
                    "容差内无可达格位"
                    if not within
                    else "容差内格位均须让给其他标记（互异格位约束）"
                ),
            }
            discarded.append(evidence)
            assignments.append(
                {
                    "id": pid,
                    "x": px,
                    "y": py,
                    "adopted": False,
                    "row": None,
                    "col": None,
                    "predicted": None,
                    "residual": None,
                    "manhattan_residual": None,
                }
            )
        else:
            assert ci not in used_cells, "两个标记占用了同一格位"
            used_cells.add(ci)
            r, c = divmod(ci, cols)
            cx, cy = cells[ci]
            dx, dy = px - cx, py - cy
            assignments.append(
                {
                    "id": pid,
                    "x": px,
                    "y": py,
                    "adopted": True,
                    "row": r,
                    "col": c,
                    "predicted": [cx, cy],
                    "residual": [dx, dy],
                    "manhattan_residual": abs(dx) + abs(dy),
                }
            )

    return {
        "solvable": True,
        "objective": {
            "discarded_count": k,
            "max_manhattan_residual": big_m,
            "total_manhattan_residual": total_s,
        },
        "parameters": {
            "origin": list(origin),
            "row_vector": list(row_vec),
            "col_vector": list(col_vec),
            "determinant": det,
            "rows": rows,
            "cols": cols,
            "tolerance": tolerance,
        },
        "assignments": assignments,
        "discarded": discarded,
    }
