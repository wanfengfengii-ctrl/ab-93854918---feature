"""候选复原结果的服务端复核。

外部工具（或设备侧）可以自行算出一组几何可行的参数与格位表，但写入对准设备
之前必须经服务端按现有四级裁决复核：

1. 弃点数（最少）；
2. 最大曼哈顿残差；
3. 曼哈顿残差总和；
4. 完整参数与按编号排列的分配序列（字典序）。

复核只接收候选声明（原点、两条基向量、每个标记的格位或弃点声明），残差与
目标值一律由本模块重新计算。结论分三类稳定原因码：

- ``infeasible``   候选违反硬约束（参数越界、det≤0、格位越界/互异、残差越限、
  弃点超限），或服务端裁决本身判定问题无解；
- ``suboptimal``   候选可行，但目标值比裁决结果差；
- ``non_canonical`` 候选可行且目标值相同，但参数或分配不是字典序规范结果。

拒绝时不回显正确分配；仅在接受时返回服务端计算所得目标值。
"""

from .solver import reconstruct

# 稳定原因码
ACCEPTED = "accepted"
INFEASIBLE = "infeasible"
SUBOPTIMAL = "suboptimal"
NON_CANONICAL = "non_canonical"


def _pair_in_bounds(v, pair):
    (lox, hix), (loy, hiy) = pair
    return lox <= v[0] <= hix and loy <= v[1] <= hiy


def audit_candidate(points, rows, cols, tolerance, max_outliers, bounds, candidate):
    """复核候选。

    points: 已按编号排序的 [(id, x, y), ...]（与 reconstruct 相同约定）。
    candidate: dict，键为 origin/row_vector/col_vector（两元序列）、
    cells=[{id, row, col}, ...]、discarded=[{id}, ...]。
    调用方须先完成“覆盖全部标记且各一次”的结构校验。

    返回::

        {"accepted": True, "objective": {...}}
        {"accepted": False, "reason_code": ..., "reason": ..., "violations": [...]}
    """
    origin = tuple(candidate["origin"])
    row_vec = tuple(candidate["row_vector"])
    col_vec = tuple(candidate["col_vector"])
    cell_claims = {c["id"]: (c["row"], c["col"]) for c in candidate["cells"]}
    discarded_ids = {d["id"] for d in candidate["discarded"]}
    point_by_id = {pid: (px, py) for pid, px, py in points}

    # ---- 服务端四级裁决（权威结果，拒绝时不对外回显） ----
    canonical = reconstruct(
        points, rows, cols, tolerance, max_outliers, bounds
    )

    # ---- 硬约束检查 ----
    violations = []

    if not _pair_in_bounds(origin, bounds["origin"]):
        violations.append("origin_out_of_bounds")
    if not _pair_in_bounds(row_vec, bounds["row_vector"]):
        violations.append("row_vector_out_of_bounds")
    if not _pair_in_bounds(col_vec, bounds["col_vector"]):
        violations.append("col_vector_out_of_bounds")

    ox, oy = origin
    ax, ay = row_vec
    bx, by = col_vec
    det = ax * by - ay * bx
    if det <= 0:
        violations.append("nonpositive_determinant")

    claimed_cells = []
    residual_ok = True
    max_mh = 0
    total_mh = 0
    for cid, (r, c) in cell_claims.items():
        if not (0 <= r < rows and 0 <= c < cols):
            violations.append("cell_out_of_grid")
            continue
        claimed_cells.append((r, c))
        # 残差由服务端按候选参数与格位重新计算
        px, py = point_by_id[cid]
        cx = ox + r * ax + c * bx
        cy = oy + r * ay + c * by
        dx, dy = px - cx, py - cy
        if abs(dx) > tolerance or abs(dy) > tolerance:
            residual_ok = False
        mh = abs(dx) + abs(dy)
        max_mh = max(max_mh, mh)
        total_mh += mh

    if len(claimed_cells) != len(set(claimed_cells)):
        violations.append("duplicate_cell")
    if not residual_ok:
        violations.append("residual_exceeds_tolerance")

    k = len(discarded_ids)
    if k > max_outliers:
        violations.append("too_many_discarded")

    if not canonical.get("solvable", False):
        # 裁决判定全局无解：任何候选都不能被接受
        return _reject(
            INFEASIBLE,
            "服务端全局裁决判定该复原请求不存在可行解，候选不能被接受。",
            violations or ["no_feasible_solution"],
        )

    if violations:
        return _reject(
            INFEASIBLE,
            "候选违反复原硬约束（参数区间、行列式、格位有效/互异、"
            "逐分量残差或弃点上限），几何上不可行。",
            violations,
        )

    cand_obj = (k, max_mh, total_mh)
    canon_obj = (
        canonical["objective"]["discarded_count"],
        canonical["objective"]["max_manhattan_residual"],
        canonical["objective"]["total_manhattan_residual"],
    )

    if cand_obj > canon_obj:
        return _reject(
            SUBOPTIMAL,
            "候选几何可行，但四级裁决目标（弃点数、最大残差、残差和）"
            "劣于服务端裁决结果。",
        )

    # 目标相同：必须与裁决的规范结果（参数 + 按编号的分配序列）逐一致
    cp = canonical["parameters"]
    params_same = (
        list(origin) == cp["origin"]
        and list(row_vec) == cp["row_vector"]
        and list(col_vec) == cp["col_vector"]
    )
    canon_map = {
        a["id"]: (a["adopted"], a["row"], a["col"])
        for a in canonical["assignments"]
    }
    assignment_same = True
    for pid in point_by_id:
        if pid in discarded_ids:
            want = (False, None, None)
        else:
            r, c = cell_claims[pid]
            want = (True, r, c)
        if canon_map[pid] != want:
            assignment_same = False
            break

    if not (params_same and assignment_same):
        return _reject(
            NON_CANONICAL,
            "候选可行且目标值与裁决结果相同，但参数或格位分配不是字典序"
            "规范结果，拒绝以保证设备侧结果唯一。",
        )

    return {
        "accepted": True,
        "objective": {
            "discarded_count": canon_obj[0],
            "max_manhattan_residual": canon_obj[1],
            "total_manhattan_residual": canon_obj[2],
        },
    }


def _reject(reason_code, reason, violations=None):
    body = {"accepted": False, "reason_code": reason_code, "reason": reason}
    if violations:
        body["violations"] = violations
    return body
