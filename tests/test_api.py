"""HTTP 层测试。"""

import os
import sys

from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.main import app  # noqa: E402

client = TestClient(app)

VB = {"x": {"lo": -3, "hi": 3}, "y": {"lo": -3, "hi": 3}}


def payload(points, **over):
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
    return [
        (r * 3 + c + 1, 2 * r, 2 * c)
        for r in range(3)
        for c in range(3)
    ]


def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "healthy"


def test_reconstruct_ok():
    r = client.post("/api/wafer-grids/reconstruct", json=payload(exact_points()))
    assert r.status_code == 200
    body = r.json()
    assert body["solvable"] is True
    assert body["parameters"]["origin"] == [0, 0]
    assert body["parameters"]["row_vector"] == [2, 0]
    assert body["parameters"]["col_vector"] == [0, 2]
    assert len({(a["row"], a["col"]) for a in body["assignments"]}) == 9
    for a in body["assignments"]:
        assert a["predicted"] == [a["x"], a["y"]]


def test_reconstruct_no_solution_body():
    pts = [(i, 100 + 3 * i, 200 + 3 * i) for i in range(1, 8)]
    r = client.post("/api/wafer-grids/reconstruct", json=payload(pts, max_outliers=2))
    assert r.status_code == 200
    body = r.json()
    assert body["solvable"] is False
    assert body["reason"]


def test_duplicate_ids_rejected():
    pts = [(1, 0, 0)] * 7
    r = client.post("/api/wafer-grids/reconstruct", json=payload(pts))
    assert r.status_code == 422


def test_interval_span_rejected():
    p = payload(exact_points())
    p["origin_bounds"] = {"x": {"lo": 0, "hi": 7}, "y": {"lo": 0, "hi": 0}}
    r = client.post("/api/wafer-grids/reconstruct", json=p)
    assert r.status_code == 422
    assert "跨度" in r.text


def test_counts_out_of_range():
    p = payload(exact_points())
    p["max_outliers"] = 3
    assert client.post("/api/wafer-grids/reconstruct", json=p).status_code == 422
    p = payload(exact_points())
    p["points"] = p["points"][:6]
    assert client.post("/api/wafer-grids/reconstruct", json=p).status_code == 422
    p = payload(exact_points())
    p["rows"] = 8
    assert client.post("/api/wafer-grids/reconstruct", json=p).status_code == 422


# ---------------------------------------------------------------------------
# POST /api/wafer-grids/audit
# ---------------------------------------------------------------------------
def audit_body(candidate, points=None, **over):
    p = payload(exact_points() if points is None else points, **over)
    return {"reconstruction_request": p, "candidate": candidate}


def canonical_candidate(points=None, **over):
    r = client.post(
        "/api/wafer-grids/reconstruct",
        json=payload(exact_points() if points is None else points, **over),
    ).json()
    cand = {
        "origin": r["parameters"]["origin"],
        "row_vector": r["parameters"]["row_vector"],
        "col_vector": r["parameters"]["col_vector"],
        "cells": [
            {"id": a["id"], "row": a["row"], "col": a["col"]}
            for a in r["assignments"]
            if a["adopted"]
        ],
        "discarded": [
            {"id": a["id"]}
            for a in r["assignments"]
            if not a["adopted"]
        ],
    }
    return cand, r


def test_audit_accepts_canonical():
    cand, r = canonical_candidate()
    resp = client.post("/api/wafer-grids/audit", json=audit_body(cand))
    assert resp.status_code == 200
    body = resp.json()
    assert body["accepted"] is True
    assert body["objective"] == r["objective"]
    # 接受响应只给目标，不回显分配
    assert "assignments" not in body


def test_audit_accepts_canonical_with_outliers():
    pts, _ = __import__("scripts.smoke", fromlist=["build_case"]).build_case()
    pts = [[i, x, y] for i, x, y in pts]
    over = {"rows": 4, "cols": 4, "max_outliers": 2, "tolerance": 1}
    cand, r = canonical_candidate(pts, **over)
    body = client.post(
        "/api/wafer-grids/audit", json=audit_body(cand, points=pts, **over)
    ).json()
    assert body["accepted"] is True
    assert body["objective"] == r["objective"] == {
        "discarded_count": 2,
        "max_manhattan_residual": 1,
        "total_manhattan_residual": 3,
    }


def test_audit_rejects_swapped_basis_as_infeasible():
    cand, _ = canonical_candidate()
    cand["row_vector"], cand["col_vector"] = cand["col_vector"], cand["row_vector"]
    resp = client.post("/api/wafer-grids/audit", json=audit_body(cand))
    assert resp.status_code == 200
    body = resp.json()
    assert body["accepted"] is False
    assert body["reason_code"] == "infeasible"
    assert "nonpositive_determinant" in body["violations"]
    assert "assignments" not in body


def test_audit_rejects_parameter_out_of_bounds():
    cand, _ = canonical_candidate()
    cand["origin"] = [9, 0]
    body = client.post("/api/wafer-grids/audit", json=audit_body(cand)).json()
    assert body["accepted"] is False
    assert body["reason_code"] == "infeasible"
    assert "origin_out_of_bounds" in body["violations"]


def test_audit_rejects_duplicate_and_out_of_range_cells():
    cand, _ = canonical_candidate()
    # 两个标记占用同一格位
    cand["cells"][1]["row"], cand["cells"][1]["col"] = (
        cand["cells"][0]["row"],
        cand["cells"][0]["col"],
    )
    body = client.post("/api/wafer-grids/audit", json=audit_body(cand)).json()
    assert body["accepted"] is False
    assert body["reason_code"] == "infeasible"
    assert "duplicate_cell" in body["violations"]

    cand2, _ = canonical_candidate()
    cand2["cells"][0]["row"] = 99  # 越界属于候选不可行，不是结构错误
    resp = client.post("/api/wafer-grids/audit", json=audit_body(cand2))
    assert resp.status_code == 200
    body = resp.json()
    assert body["reason_code"] == "infeasible"
    assert "cell_out_of_grid" in body["violations"]


def test_audit_rejects_residual_beyond_tolerance():
    # 容差 0 的精确栅格：把一个标记移到另一格位即产生越限残差
    cand, _ = canonical_candidate()
    cand["cells"][0]["row"], cand["cells"][0]["col"] = 0, 1
    cand["cells"][1]["row"], cand["cells"][1]["col"] = 0, 0
    body = client.post("/api/wafer-grids/audit", json=audit_body(cand)).json()
    assert body["accepted"] is False
    assert body["reason_code"] == "infeasible"
    assert "residual_exceeds_tolerance" in body["violations"]


def test_audit_rejects_too_many_discarded():
    # 容差 0、允许弃 0：把一个精确点声明为弃点 → 弃点超限不可行
    cand, _ = canonical_candidate()
    victim = cand["cells"].pop()
    cand["discarded"].append({"id": victim["id"]})
    body = client.post("/api/wafer-grids/audit", json=audit_body(cand)).json()
    assert body["accepted"] is False
    assert body["reason_code"] == "infeasible"
    assert "too_many_discarded" in body["violations"]


def test_audit_rejects_suboptimal_candidate():
    # 精确栅格允许弃 1：弃掉一个本可精确归位的点 → 可行但次优
    cand, _ = canonical_candidate(max_outliers=1, tolerance=1)
    victim = cand["cells"].pop(0)
    cand["discarded"].append({"id": victim["id"]})
    body = client.post(
        "/api/wafer-grids/audit",
        json=audit_body(cand, max_outliers=1, tolerance=1),
    ).json()
    assert body["accepted"] is False
    assert body["reason_code"] == "suboptimal"
    assert "assignments" not in body


def test_audit_rejects_non_canonical_equal_objective():
    # 两个标记同在 (1,0)：交换 (0,0)/(1,0) 的占用，目标相同但非字典序规范
    pts = [(1, 1, 0), (2, 1, 0)]
    pid = 3
    for r in range(3):
        for c in range(3):
            if (r, c) in ((0, 0), (1, 0)):
                continue
            pts.append((pid, 2 * r, 2 * c))
            pid += 1
    over = {"tolerance": 1, "max_outliers": 2}
    cand, r = canonical_candidate(pts, **over)
    assert r["objective"]["total_manhattan_residual"] == 2
    # 规范候选通过
    ok = client.post(
        "/api/wafer-grids/audit", json=audit_body(cand, points=pts, **over)
    ).json()
    assert ok["accepted"] is True
    # 交换两个标记的格位
    for cell in cand["cells"]:
        if cell["id"] == 1:
            cell["row"], cell["col"] = 1, 0
        if cell["id"] == 2:
            cell["row"], cell["col"] = 0, 0
    body = client.post(
        "/api/wafer-grids/audit", json=audit_body(cand, points=pts, **over)
    ).json()
    assert body["accepted"] is False
    assert body["reason_code"] == "non_canonical"
    assert "assignments" not in body


def test_audit_infeasible_when_no_solution_exists():
    # 整体无解时，即便候选形式上自洽也必须拒绝
    far = [(i, 100 + 3 * i, 200 + 3 * i) for i in range(1, 8)]
    cand = {
        "origin": [0, 0],
        "row_vector": [2, 0],
        "col_vector": [0, 2],
        "cells": [{"id": i, "row": 0, "col": 0} for i in range(1, 8)],
        "discarded": [],
    }
    # 上述格位互异校验需要互异：改为前 7 个不同格位，残差必越限
    cand["cells"] = [
        {"id": i, "row": (i - 1) // 3, "col": (i - 1) % 3} for i in range(1, 8)
    ]
    body = client.post(
        "/api/wafer-grids/audit", json=audit_body(cand, points=far, max_outliers=2)
    ).json()
    assert body["accepted"] is False
    assert body["reason_code"] == "infeasible"


def test_audit_missing_marker_returns_field_level_422():
    cand, _ = canonical_candidate()
    cand["cells"].pop()  # 少覆盖一个标记
    resp = client.post("/api/wafer-grids/audit", json=audit_body(cand))
    assert resp.status_code == 422
    text = resp.text
    assert "candidate" in text and "未被候选覆盖" in text


def test_audit_duplicate_marker_claim_returns_422():
    cand, _ = canonical_candidate()
    dup = dict(cand["cells"][0])
    cand["cells"].append(dup)  # 同一标记声明两次
    resp = client.post("/api/wafer-grids/audit", json=audit_body(cand))
    assert resp.status_code == 422
    assert "重复出现" in resp.text


def test_audit_unknown_and_overlapping_marker_returns_422():
    cand, _ = canonical_candidate()
    claim = cand["cells"][0]
    cand["discarded"].append({"id": claim["id"]})  # 格位与弃点同时声明
    cand["discarded"].append({"id": 777})  # 未知标记
    resp = client.post("/api/wafer-grids/audit", json=audit_body(cand))
    assert resp.status_code == 422
    assert "只能出现一次" in resp.text
    assert "不存在的标记 777" in resp.text


def test_audit_bad_embedded_request_returns_422():
    bad = audit_body(canonical_candidate()[0])
    bad["reconstruction_request"]["rows"] = 99
    assert client.post("/api/wafer-grids/audit", json=bad).status_code == 422
    bad2 = audit_body(canonical_candidate()[0])
    del bad2["candidate"]["discarded"]
    assert client.post("/api/wafer-grids/audit", json=bad2).status_code == 422
