"""FastAPI 应用：晶圆标记栅格复原服务。"""

from fastapi import FastAPI
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, Field, model_validator

from .audit import audit_candidate
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


class CandidateCell(BaseModel):
    """单个标记的格位声明（行主序，行列均为 0 基）。"""

    id: int = Field(..., description="标记编号，须与 reconstruction_request 一致")
    row: int = Field(..., description="格位行号；越界属于候选不可行而非结构错误")
    col: int


class CandidateDiscard(BaseModel):
    """单个标记的弃点声明。"""

    id: int


class Candidate(BaseModel):
    """待复核候选：参数 + 每个标记的格位或弃点声明。

    残差与目标值不由调用方提交，一律由服务端重新计算。
    """

    origin: list[int] = Field(..., min_length=2, max_length=2)
    row_vector: list[int] = Field(..., min_length=2, max_length=2)
    col_vector: list[int] = Field(..., min_length=2, max_length=2)
    cells: list[CandidateCell] = Field(..., description="采用标记的格位声明")
    discarded: list[CandidateDiscard] = Field(
        ..., description="弃点标记声明，数量不得超过 max_outliers"
    )


class AuditRequest(BaseModel):
    reconstruction_request: ReconstructRequest
    candidate: Candidate


def _bounds_pair(vb: VectorBounds):
    return ([vb.x.lo, vb.x.hi], [vb.y.lo, vb.y.hi])


def _solver_inputs(req: ReconstructRequest):
    # 按编号排序：字典序决胜项定义在“按编号排列的分配序列”上
    points = sorted(((p.id, p.x, p.y) for p in req.points), key=lambda t: t[0])
    bounds = {
        "origin": _bounds_pair(req.origin_bounds),
        "row_vector": _bounds_pair(req.row_vector_bounds),
        "col_vector": _bounds_pair(req.col_vector_bounds),
    }
    return points, bounds


def _candidate_coverage_errors(req: AuditRequest):
    """结构校验：候选须覆盖全部标记，且每个标记恰好出现一次（格位或弃点）。

    返回字段级错误列表（loc 精确到 cells/discarded 及重复编号）。
    """
    marker_ids = [p.id for p in req.reconstruction_request.points]
    marker_set = set(marker_ids)
    errors = []

    cell_ids = [c.id for c in req.candidate.cells]
    discard_ids = [d.id for d in req.candidate.discarded]

    for name, ids in (("cells", cell_ids), ("discarded", discard_ids)):
        seen = set()
        for i, cid in enumerate(ids):
            if cid in seen:
                errors.append(
                    {
                        "loc": ("body", "candidate", name, i, "id"),
                        "msg": f"标记 {cid} 在候选 {name} 中重复出现",
                        "type": "value_error",
                    }
                )
            seen.add(cid)

    overlap = set(cell_ids) & set(discard_ids)
    for cid in sorted(overlap):
        errors.append(
            {
                "loc": ("body", "candidate"),
                "msg": f"标记 {cid} 同时声明为格位与弃点，每个标记只能出现一次",
                "type": "value_error",
            }
        )

    declared = set(cell_ids) | set(discard_ids)
    unknown = sorted(declared - marker_set)
    missing = sorted(marker_set - declared)
    for cid in unknown:
        errors.append(
            {
                "loc": ("body", "candidate"),
                "msg": f"候选声明了 reconstruction_request 中不存在的标记 {cid}",
                "type": "value_error",
            }
        )
    for cid in missing:
        errors.append(
            {
                "loc": ("body", "candidate"),
                "msg": f"标记 {cid} 未被候选覆盖：须恰好声明一次格位或弃点",
                "type": "value_error",
            }
        )
    return errors


@app.get("/health")
async def health():
    return {"status": "healthy"}


@app.post("/api/wafer-grids/reconstruct")
async def reconstruct_grid(req: ReconstructRequest):
    points, bounds = _solver_inputs(req)
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
    """复核外部提交的候选复原结果，避免未遵循全局裁决的参数/格位表写入设备。

    结构错误（候选未覆盖全部标记或某标记出现多次、声明未知标记等）返回
    字段级 422；结构合法时恒返回 200，以 accepted 标识结论：
    不可行 / 次优 / 同目标但非规范均以稳定原因码拒绝，且不回显正确分配。
    """
    coverage_errors = _candidate_coverage_errors(req)
    if coverage_errors:
        raise RequestValidationError(coverage_errors)

    rr = req.reconstruction_request
    points, bounds = _solver_inputs(rr)
    candidate = {
        "origin": req.candidate.origin,
        "row_vector": req.candidate.row_vector,
        "col_vector": req.candidate.col_vector,
        "cells": [c.model_dump() for c in req.candidate.cells],
        "discarded": [d.model_dump() for d in req.candidate.discarded],
    }
    return await run_in_threadpool(
        audit_candidate,
        points,
        rr.rows,
        rr.cols,
        rr.tolerance,
        rr.max_outliers,
        bounds,
        candidate,
    )
