"""审计冒烟：成功复核 + 三类拒绝（不可行 / 次优 / 同优但不规范）+ 结构错误。

可直接运行（TestClient，不依赖服务）；verify 流程在服务健康后通过 BASE_URL
走 HTTP。用法::

    python scripts/audit_smoke.py            # 直测应用（TestClient）
    BASE_URL=http://web:8000 python scripts/audit_smoke.py   # 走 HTTP
"""

import copy
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.smoke import build_case, expected_payload  # noqa: E402

BASE_URL = os.environ.get("BASE_URL")
_client = None


def _post(path, payload):
    """POST JSON，返回 (status, body)；有 BASE_URL 走 HTTP，否则用 TestClient。"""
    global _client
    if BASE_URL:
        import urllib.error
        import urllib.request

        req = urllib.request.Request(
            BASE_URL.rstrip("/") + path,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())
    if _client is None:
        from fastapi.testclient import TestClient

        from app.main import app

        _client = TestClient(app)
    resp = _client.post(path, json=payload)
    return resp.status_code, resp.json()


def _audit(recon_req, candidate):
    return _post(
        "/api/wafer-grids/audit",
        {"reconstruction_request": recon_req, "candidate": candidate},
    )


def _canonical_candidate(result):
    """把 reconstruct 的规范结果转成候选格式（设备侧的正常行为）。"""
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


def _expect_reject(req, candidate, category, code):
    status, body = _audit(req, candidate)
    assert status == 200, (code, body)
    assert body["accepted"] is False, body
    assert body["category"] == category, body
    assert body["reason_code"] == code, body
    # 拒绝响应不得回显正确分配/参数
    assert "assignments" not in body and "parameters" not in body, body
    print(f"  [拒绝:{category}/{code}] {body['message']}")
    return body


def case_accept():
    """规范候选 → accepted=true，目标与重建结果一致。"""
    points, _ = build_case()
    req = expected_payload(points)
    status, result = _post("/api/wafer-grids/reconstruct", req)
    assert status == 200 and result["solvable"], result
    status, body = _audit(req, _canonical_candidate(result))
    assert status == 200, body
    assert body["accepted"] is True, body
    assert body["objective"] == result["objective"], body
    obj = body["objective"]
    print(
        f"  [通过] 规范候选被接受，目标 = (k={obj['discarded_count']}, "
        f"max={obj['max_manhattan_residual']}, sum={obj['total_manhattan_residual']})"
    )


def case_infeasible():
    """六类不可行拒绝：参数越界/行列式非正/格位越界/格位重复/残差越容差/弃点超限。"""
    points, _ = build_case()
    req = expected_payload(points)
    _, result = _post("/api/wafer-grids/reconstruct", req)
    canon = _canonical_candidate(result)

    cand = copy.deepcopy(canon)
    cand["origin"] = [4, 0]  # 区间 [-3, 3] 之外
    _expect_reject(req, cand, "infeasible", "PARAMETERS_OUT_OF_BOUNDS")

    cand = copy.deepcopy(canon)
    cand["row_vector"], cand["col_vector"] = cand["col_vector"], cand["row_vector"]
    _expect_reject(req, cand, "infeasible", "NON_POSITIVE_DETERMINANT")

    cand = copy.deepcopy(canon)
    cand["assignments"][0]["row"] = 4  # rows=4，合法行号 0..3
    _expect_reject(req, cand, "infeasible", "CELL_OUT_OF_RANGE")

    cand = copy.deepcopy(canon)
    cand["assignments"][0]["row"] = cand["assignments"][1]["row"]
    cand["assignments"][0]["col"] = cand["assignments"][1]["col"]
    _expect_reject(req, cand, "infeasible", "DUPLICATE_CELL")

    cand = copy.deepcopy(canon)
    cand["origin"] = [2, 0]  # 仍在区间内，但残差整体平移、越过容差 1
    _expect_reject(req, cand, "infeasible", "RESIDUAL_EXCEEDS_TOLERANCE")

    cand = copy.deepcopy(canon)
    for entry in cand["assignments"][:3]:  # 全部改判弃点：3 > max_outliers=2
        entry.pop("row", None)
        entry.pop("col", None)
        entry["discarded"] = True
    _expect_reject(req, cand, "infeasible", "DISCARD_LIMIT_EXCEEDED")


def _small_request(points, tolerance):
    vb = {"x": {"lo": -3, "hi": 3}, "y": {"lo": -3, "hi": 3}}
    return {
        "points": [{"id": i, "x": x, "y": y} for i, x, y in points],
        "rows": 3,
        "cols": 3,
        "max_outliers": 0,
        "tolerance": tolerance,
        "origin_bounds": vb,
        "row_vector_bounds": vb,
        "col_vector_bounds": vb,
    }


def case_suboptimal():
    """几何可行但目标较差：原点平移 (1,1)，目标 (0,2,18) 劣于规范 (0,0,0)。"""
    points = [(r * 3 + c + 1, 2 * r, 2 * c) for r in range(3) for c in range(3)]
    req = _small_request(points, tolerance=1)
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
    body = _expect_reject(req, cand, "suboptimal", "SUBOPTIMAL_OBJECTIVE")
    assert body["objective"] == {
        "discarded_count": 0,
        "max_manhattan_residual": 2,
        "total_manhattan_residual": 18,
    }, body


def case_non_canonical():
    """同目标但非规范：中心对称栅格的 90° 旋转参数组合同为 (0,0,0)。"""
    points = [(r * 3 + c + 1, 2 * r - 2, 2 * c - 2) for r in range(3) for c in range(3)]
    req = _small_request(points, tolerance=0)
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
    assert len(assignments) == 9
    cand = {
        "origin": list(origin),
        "row_vector": list(av),
        "col_vector": list(bv),
        "assignments": assignments,
    }
    body = _expect_reject(req, cand, "non_canonical", "NON_CANONICAL")
    assert body["objective"] == {
        "discarded_count": 0,
        "max_manhattan_residual": 0,
        "total_manhattan_residual": 0,
    }, body


def case_structural_422():
    """结构错误 → 字段级 422：缺标记、编号重复、采用声明缺格位。"""
    points, _ = build_case()
    req = expected_payload(points)
    _, result = _post("/api/wafer-grids/reconstruct", req)
    canon = _canonical_candidate(result)

    cand = copy.deepcopy(canon)
    cand["assignments"] = cand["assignments"][:-1]
    status, _ = _audit(req, cand)
    assert status == 422, status

    cand = copy.deepcopy(canon)
    cand["assignments"].append(copy.deepcopy(cand["assignments"][0]))
    status, _ = _audit(req, cand)
    assert status == 422, status

    cand = copy.deepcopy(canon)
    cand["assignments"][0] = {"id": cand["assignments"][0]["id"]}
    status, _ = _audit(req, cand)
    assert status == 422, status
    print("  [拒绝:422] 缺标记 / 编号重复 / 采用声明缺格位均返回字段级 422")


def main():
    where = f"HTTP ({BASE_URL})" if BASE_URL else "TestClient 直测"
    print(f"==> 审计冒烟（{where}）")
    case_accept()
    case_infeasible()
    case_suboptimal()
    case_non_canonical()
    case_structural_422()
    print("==> 审计冒烟通过：成功复核与不可行/次优/同优非规范三类拒绝均正确")


if __name__ == "__main__":
    main()
