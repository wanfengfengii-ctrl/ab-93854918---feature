"""审计端点测试：成功复核 + 不可行 / 次优 / 同优非规范三类拒绝 + 结构错误。"""

import copy
import os
import sys

from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.main import app  # noqa: E402

client = TestClient(app)

VB = {"x": {"lo": -3, "hi": 3}, "y": {"lo": -3, "hi": 3}}


def recon_payload(points, **over):
    base = {
        "points": [{"id": i, "x": x, "y": y} for i, x, y in points],
        "rows": 3,
        "cols": 3,
        "max_outliers": 0,
        "tolerance": 0,
        "origin_bounds": VB,
        "row_vector_bounds": VB,
        "col_vector_bounds": VB,
    }
    base.update(over)
    return base


def exact_points():
    """3x3 精确栅格：O=(0,0)，A=(2,0)，B=(0,2)。"""
    return [(r * 3 + c + 1, 2 * r, 2 * c) for r in range(3) for c in range(3)]


def solve(req):
    r = client.post("/api/wafer-grids/reconstruct", json=req)
    assert r.status_code == 200 and r.json()["solvable"], r.json()
    return r.json()


def canonical_candidate(result):
    p = result["parameters"]
    return {
        "origin": p["origin"],
        "row_vector": p["row_vector"],
        "col_vector": p["col_vector"],
        "assignments": [
            (
                {"id": a["id"], "row": a["row"], "col": a["col"]}
                if a["adopted"]
                else {"id": a["id"], "discarded": True}
            )
            for a in result["assignments"]
        ],
    }


def audit(req, candidate):
    return client.post(
        "/api/wafer-grids/audit",
        json={"reconstruction_request": req, "candidate": candidate},
    )


def expect_reject(req, candidate, category, code):
    r = audit(req, candidate)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["accepted"] is False, body
    assert body["category"] == category, body
    assert body["reason_code"] == code, body
    # 拒绝响应不得回显正确分配/参数
    assert "assignments" not in body and "parameters" not in body
    return body


# ---------------------------------------------------------------------------
# 接受
# ---------------------------------------------------------------------------
def test_audit_accepts_canonical_candidate():
    req = recon_payload(exact_points())
    result = solve(req)
    r = audit(req, canonical_candidate(result))
    assert r.status_code == 200
    body = r.json()
    assert body["accepted"] is True
    assert body["objective"] == result["objective"] == {
        "discarded_count": 0,
        "max_manhattan_residual": 0,
        "total_manhattan_residual": 0,
    }


def test_audit_accepts_canonical_with_discards():
    from scripts.smoke import build_case, expected_payload

    points, _ = build_case()
    req = expected_payload(points)
    result = solve(req)
    assert result["objective"]["discarded_count"] == 2
    r = audit(req, canonical_candidate(result))
    assert r.status_code == 200
    body = r.json()
    assert body["accepted"] is True
    assert body["objective"] == result["objective"]


# ---------------------------------------------------------------------------
# 不可行（infeasible）
# ---------------------------------------------------------------------------
def test_audit_rejects_parameters_out_of_bounds():
    req = recon_payload(exact_points())
    cand = canonical_candidate(solve(req))
    cand["origin"] = [4, 0]  # 区间 [-3, 3] 之外
    body = expect_reject(req, cand, "infeasible", "PARAMETERS_OUT_OF_BOUNDS")
    assert body["objective"] is None


def test_audit_rejects_non_positive_determinant():
    req = recon_payload(exact_points())
    cand = canonical_candidate(solve(req))
    cand["row_vector"], cand["col_vector"] = cand["col_vector"], cand["row_vector"]
    expect_reject(req, cand, "infeasible", "NON_POSITIVE_DETERMINANT")


def test_audit_rejects_cell_out_of_range():
    req = recon_payload(exact_points())
    cand = canonical_candidate(solve(req))
    cand["assignments"][0]["row"] = 3  # rows=3，合法行号 0..2
    expect_reject(req, cand, "infeasible", "CELL_OUT_OF_RANGE")


def test_audit_rejects_duplicate_cell():
    req = recon_payload(exact_points())
    cand = canonical_candidate(solve(req))
    # 标记 1 与标记 2 占用同一格位
    cand["assignments"][0]["row"] = cand["assignments"][1]["row"]
    cand["assignments"][0]["col"] = cand["assignments"][1]["col"]
    expect_reject(req, cand, "infeasible", "DUPLICATE_CELL")


def test_audit_rejects_residual_exceeds_tolerance():
    req = recon_payload(exact_points())
    cand = canonical_candidate(solve(req))
    # 原点平移 (1,0)（仍在区间内）：所有残差变为 (-1,0)，越过容差 0
    cand["origin"] = [1, 0]
    body = expect_reject(req, cand, "infeasible", "RESIDUAL_EXCEEDS_TOLERANCE")
    assert body["details"]["id"] == 1


def test_audit_rejects_discard_limit_exceeded():
    req = recon_payload(exact_points(), max_outliers=1)
    cand = canonical_candidate(solve(req))
    # 把两个采用标记改判弃点：弃点数 2 > 上限 1
    for entry in cand["assignments"][:2]:
        entry.pop("row")
        entry.pop("col")
        entry["discarded"] = True
    expect_reject(req, cand, "infeasible", "DISCARD_LIMIT_EXCEEDED")


# ---------------------------------------------------------------------------
# 次优（suboptimal）
# ---------------------------------------------------------------------------
def test_audit_rejects_suboptimal_objective():
    # 精确栅格 + 容差 1：原点平移 (1,1) 后残差 (-1,-1) 仍可行，
    # 目标 (0, 2, 18) 劣于规范解 (0, 0, 0)
    req = recon_payload(exact_points(), tolerance=1)
    cand = {
        "origin": [1, 1],
        "row_vector": [2, 0],
        "col_vector": [0, 2],
        "assignments": [
            {"id": r * 3 + c + 1, "row": r, "col": c}
            for r in range(3)
            for c in range(3)
        ],
    }
    body = expect_reject(req, cand, "suboptimal", "SUBOPTIMAL_OBJECTIVE")
    assert body["objective"] == {
        "discarded_count": 0,
        "max_manhattan_residual": 2,
        "total_manhattan_residual": 18,
    }


# ---------------------------------------------------------------------------
# 同目标但非规范（non_canonical）
# ---------------------------------------------------------------------------
def test_audit_rejects_same_objective_non_canonical():
    # 中心对称 3x3 栅格：旋转 90° 的参数组合同为 (0,0,0)，
    # 但规范解是字典序最小的 O=(-2,-2), A=(2,0), B=(0,2)
    points = [(r * 3 + c + 1, 2 * r - 2, 2 * c - 2) for r in range(3) for c in range(3)]
    req = recon_payload(points)
    origin, av, bv = (2, -2), (0, 2), (-2, 0)
    assignments = []
    for pid, x, y in points:
        for r in range(3):
            for c in range(3):
                if (
                    origin[0] + r * av[0] + c * bv[0],
                    origin[1] + r * av[1] + c * bv[1],
                ) == (x, y):
                    assignments.append({"id": pid, "row": r, "col": c})
    cand = {
        "origin": list(origin),
        "row_vector": list(av),
        "col_vector": list(bv),
        "assignments": assignments,
    }
    body = expect_reject(req, cand, "non_canonical", "NON_CANONICAL")
    # 候选自身目标确实与最优一致（同优但不规范）
    assert body["objective"] == {
        "discarded_count": 0,
        "max_manhattan_residual": 0,
        "total_manhattan_residual": 0,
    }


# ---------------------------------------------------------------------------
# 结构错误（422，字段级）
# ---------------------------------------------------------------------------
def test_audit_422_when_marker_missing():
    req = recon_payload(exact_points())
    cand = canonical_candidate(solve(req))
    cand["assignments"] = cand["assignments"][:-1]
    r = audit(req, cand)
    assert r.status_code == 422
    assert "缺少" in r.text


def test_audit_422_when_marker_duplicated():
    req = recon_payload(exact_points())
    cand = canonical_candidate(solve(req))
    cand["assignments"].append(copy.deepcopy(cand["assignments"][0]))
    r = audit(req, cand)
    assert r.status_code == 422
    assert "重复" in r.text


def test_audit_422_when_extra_marker():
    req = recon_payload(exact_points())
    cand = canonical_candidate(solve(req))
    cand["assignments"].append({"id": 999, "discarded": True})
    r = audit(req, cand)
    assert r.status_code == 422
    assert "多余" in r.text


def test_audit_422_when_adopted_entry_lacks_cell():
    req = recon_payload(exact_points())
    cand = canonical_candidate(solve(req))
    cand["assignments"][0] = {"id": cand["assignments"][0]["id"]}
    assert audit(req, cand).status_code == 422


def test_audit_422_when_discarded_entry_carries_cell():
    req = recon_payload(exact_points())
    cand = canonical_candidate(solve(req))
    cand["assignments"][0]["discarded"] = True  # 仍带 row/col
    assert audit(req, cand).status_code == 422


def test_audit_422_when_reconstruction_request_invalid():
    req = recon_payload(exact_points())
    req["max_outliers"] = 3  # 超出 0..2
    cand = canonical_candidate(solve(recon_payload(exact_points())))
    assert audit(req, cand).status_code == 422
