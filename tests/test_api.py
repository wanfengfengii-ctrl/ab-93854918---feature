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
