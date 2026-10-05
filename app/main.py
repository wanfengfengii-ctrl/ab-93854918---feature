"""FastAPI 应用：晶圆标记栅格复原服务。"""

from fastapi import FastAPI, HTTPException
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field, model_validator

from .solver import reconstruct

app = FastAPI(
    title="Wafer Grid Reconstruction API",
    description="从带编号的无序整数坐标中恢复栅格原点、基向量与格位分配。",
    version="1.1.0",
)


class Marker(BaseModel):
    id: int = Field(..., description="标记唯一编号")
    x: int
    y: int


class Interval(BaseModel):
    """单个分量的闭区间 [lo, hi]，跨度不超过 6。"""

    lo: int
    hi: int

    @model_validator(mode="after")
    def _check(self):
        if self.hi < self.lo:
            raise ValueError(f"闭区间下界 {self.lo} 不得大于上界 {self.hi}")
        if self.hi - self.lo > 6:
            raise ValueError(
                f"区间 [{self.lo}, {self.hi}] 跨度 {self.hi - self.lo} 超过 6"
            )
        return self


class VectorBounds(BaseModel):
    x: Interval
    y: Interval


class ReconstructRequest(BaseModel):
    points: list[Marker] = Field(..., min_length=7, max_length=14)
    rows: int = Field(..., ge=3, le=7)
    cols: int = Field(..., ge=3, le=7)
    max_outliers: int = Field(..., ge=0, le=2)
    tolerance: int = Field(..., ge=0, description="逐分量（L∞）坐标容差")
    origin_bounds: VectorBounds
    row_vector_bounds: VectorBounds
    col_vector_bounds: VectorBounds

    @model_validator(mode="after")
    def _check_points(self):
        ids = [p.id for p in self.points]
        if len(set(ids)) != len(ids):
            raise ValueError("标记编号必须唯一")
        return self


def _bounds_pair(vb: VectorBounds):
    return ([vb.x.lo, vb.x.hi], [vb.y.lo, vb.y.hi])


# ---------------------------------------------------------------------------
# 审计（设备提交的候选复原结果复核）
# ---------------------------------------------------------------------------
class CandidateAssignment(BaseModel):
    """候选分配条目：采用（给出格位 row/col）或弃点（discarded=true）。"""

    id: int
    row: int | None = None
    col: int | None = None
    discarded: bool = False

    @model_validator(mode="after")
    def _check(self):
        if self.discarded:
            if self.row is not None or self.col is not None:
                raise ValueError("弃点声明不得同时给出格位 row/col")
        elif self.row is None or self.col is None:
            raise ValueError("采用声明必须给出格位 row 与 col")
        return self


class Candidate(BaseModel):
    """设备提交的候选复原结果；残差与目标值由服务端计算，不在此提交。"""

    origin: list[int] = Field(..., min_length=2, max_length=2)
    row_vector: list[int] = Field(..., min_length=2, max_length=2)
    col_vector: list[int] = Field(..., min_length=2, max_length=2)
    assignments: list[CandidateAssignment]


class AuditRequest(BaseModel):
    reconstruction_request: ReconstructRequest
    candidate: Candidate

    @model_validator(mode="after")
    def _check_coverage(self):
        want = [p.id for p in self.reconstruction_request.points]
        got = [a.id for a in self.candidate.assignments]
        dup = sorted({i for i in got if got.count(i) > 1})
        if dup:
            raise ValueError(f"候选中标记编号重复出现：{dup}")
        missing = sorted(set(want) - set(got))
        extra = sorted(set(got) - set(want))
        if missing or extra:
            raise ValueError(
                "候选须覆盖全部标记且各出现一次："
                f"缺少 {missing or '无'}，多余 {extra or '无'}"
            )
        return self


def _reject(category, code, message, objective=None, details=None):
    """构造拒绝响应：稳定原因码 + 类别，绝不回显规范参数与分配。"""
    body = {
        "accepted": False,
        "category": category,
        "reason_code": code,
        "message": message,
        "objective": objective,
    }
    if details is not None:
        body["details"] = details
    return body


@app.get("/health")
async def health():
    return {"status": "healthy"}


@app.post("/api/wafer-grids/reconstruct")
async def reconstruct_grid(req: ReconstructRequest):
    # 按编号排序：字典序决胜项定义在“按编号排列的分配序列”上
    points = sorted(((p.id, p.x, p.y) for p in req.points), key=lambda t: t[0])
    bounds = {
        "origin": _bounds_pair(req.origin_bounds),
        "row_vector": _bounds_pair(req.row_vector_bounds),
        "col_vector": _bounds_pair(req.col_vector_bounds),
    }
    result = await run_in_threadpool(
        reconstruct,
        points,
        req.rows,
        req.cols,
        req.tolerance,
        req.max_outliers,
        bounds,
    )
    if not result.get("solvable"):
        # 几何上无解不是请求格式错误：以 200 返回明确的无解原因，
        # 由响应体 solvable=false 标识。
        return result
    return result


@app.post("/api/wafer-grids/audit")
async def audit_grid(req: AuditRequest):
    """复核设备提交的候选复原结果。

    服务端重新计算残差与目标值，并按现有四级裁决重求规范解比对：
    一致返回 accepted=true 与计算所得目标；候选不可行、目标较差或同目标
    但非规范结果返回 accepted=false 与稳定原因码（不回显正确分配）。
    """
    recon = req.reconstruction_request
    cand = req.candidate
    rows, cols = recon.rows, recon.cols
    ox, oy = cand.origin
    ax, ay = cand.row_vector
    bx, by = cand.col_vector

    # 1) 参数位于原区间
    for name, (vx, vy), vb in (
        ("origin", (ox, oy), recon.origin_bounds),
        ("row_vector", (ax, ay), recon.row_vector_bounds),
        ("col_vector", (bx, by), recon.col_vector_bounds),
    ):
        if not (vb.x.lo <= vx <= vb.x.hi and vb.y.lo <= vy <= vb.y.hi):
            return _reject(
                "infeasible",
                "PARAMETERS_OUT_OF_BOUNDS",
                f"候选参数 {name}=({vx}, {vy}) 超出原区间 "
                f"x∈[{vb.x.lo}, {vb.x.hi}]、y∈[{vb.y.lo}, {vb.y.hi}]",
                details={"parameter": name, "value": [vx, vy]},
            )

    # 2) 行列式为正
    det = ax * by - ay * bx
    if det <= 0:
        return _reject(
            "infeasible",
            "NON_POSITIVE_DETERMINANT",
            f"候选基向量行列式 det={det}，必须为正",
            details={"determinant": det},
        )

    # 3) 采用标记的格位有效且互异
    points_by_id = {p.id: (p.x, p.y) for p in recon.points}
    owner = {}
    for a in cand.assignments:
        if a.discarded:
            continue
        if not (0 <= a.row < rows and 0 <= a.col < cols):
            return _reject(
                "infeasible",
                "CELL_OUT_OF_RANGE",
                f"标记 {a.id} 的格位 ({a.row}, {a.col}) 超出 "
                f"{rows} 行 {cols} 列栅格",
                details={"id": a.id, "row": a.row, "col": a.col},
            )
        key = (a.row, a.col)
        if key in owner:
            return _reject(
                "infeasible",
                "DUPLICATE_CELL",
                f"格位 ({a.row}, {a.col}) 被标记 {owner[key]} 与 {a.id} 重复占用",
                details={"row": a.row, "col": a.col, "ids": [owner[key], a.id]},
            )
        owner[key] = a.id

    # 4) 逐分量残差不越过容差，同时由服务端计算目标值
    tol = recon.tolerance
    discarded_count = 0
    max_mh = 0
    total_mh = 0
    for a in cand.assignments:
        if a.discarded:
            discarded_count += 1
            continue
        px, py = points_by_id[a.id]
        cx = ox + a.row * ax + a.col * bx
        cy = oy + a.row * ay + a.col * by
        dx, dy = px - cx, py - cy
        if abs(dx) > tol or abs(dy) > tol:
            return _reject(
                "infeasible",
                "RESIDUAL_EXCEEDS_TOLERANCE",
                f"标记 {a.id} 在格位 ({a.row}, {a.col}) 的残差 ({dx}, {dy}) "
                f"越过逐分量容差 {tol}",
                details={"id": a.id, "residual": [dx, dy], "tolerance": tol},
            )
        mh = abs(dx) + abs(dy)
        if mh > max_mh:
            max_mh = mh
        total_mh += mh

    # 5) 弃点数不得超过原上限
    if discarded_count > recon.max_outliers:
        return _reject(
            "infeasible",
            "DISCARD_LIMIT_EXCEEDED",
            f"候选弃点 {discarded_count} 个，超过上限 {recon.max_outliers}",
            details={
                "discarded_count": discarded_count,
                "max_outliers": recon.max_outliers,
            },
        )

    objective = {
        "discarded_count": discarded_count,
        "max_manhattan_residual": max_mh,
        "total_manhattan_residual": total_mh,
    }

    # 6) 服务端按现有四级裁决重求规范解（候选已过可行性检查，必然有解）
    points = sorted(((p.id, p.x, p.y) for p in recon.points), key=lambda t: t[0])
    bounds = {
        "origin": _bounds_pair(recon.origin_bounds),
        "row_vector": _bounds_pair(recon.row_vector_bounds),
        "col_vector": _bounds_pair(recon.col_vector_bounds),
    }
    result = await run_in_threadpool(
        reconstruct, points, rows, cols, tol, recon.max_outliers, bounds
    )
    if not result.get("solvable"):
        # 可行候选存在则问题必然可解；此为防御性分支
        raise HTTPException(status_code=500, detail="候选可行但未求得规范解")

    canon = result["objective"]
    canon_key = (
        canon["discarded_count"],
        canon["max_manhattan_residual"],
        canon["total_manhattan_residual"],
    )
    cand_key = (discarded_count, max_mh, total_mh)
    if cand_key > canon_key:
        return _reject(
            "suboptimal",
            "SUBOPTIMAL_OBJECTIVE",
            f"候选目标 (弃点={cand_key[0]}, 最大残差={cand_key[1]}, "
            f"残差和={cand_key[2]}) 劣于服务端按四级裁决求得的最优目标",
            objective=objective,
        )
    if cand_key < canon_key:
        # 规范解是最优解，候选不可能更优；此为防御性分支
        raise HTTPException(status_code=500, detail="候选目标优于规范解，内部不一致")

    # 7) 目标相同：参数与按编号排列的分配序列均须为规范（字典序最小）结果
    cp = result["parameters"]
    canon_params = (
        cp["origin"][0],
        cp["origin"][1],
        cp["row_vector"][0],
        cp["row_vector"][1],
        cp["col_vector"][0],
        cp["col_vector"][1],
    )
    if (ox, oy, ax, ay, bx, by) != canon_params:
        return _reject(
            "non_canonical",
            "NON_CANONICAL",
            "候选目标与最优一致，但参数不是规范（字典序最小）结果",
            objective=objective,
        )
    canon_assign = {
        a["id"]: (-1 if not a["adopted"] else a["row"] * cols + a["col"])
        for a in result["assignments"]
    }
    cand_assign = {
        a.id: (-1 if a.discarded else a.row * cols + a.col)
        for a in cand.assignments
    }
    if cand_assign != canon_assign:
        return _reject(
            "non_canonical",
            "NON_CANONICAL",
            "候选目标与最优一致，但分配不是规范（按编号字典序最小）结果",
            objective=objective,
        )

    return {"accepted": True, "objective": objective}
