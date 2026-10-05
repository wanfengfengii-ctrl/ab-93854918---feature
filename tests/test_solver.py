"""核心求解器测试。"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.solver import reconstruct  # noqa: E402

BOUNDS_WIDE = {
    "origin": ([-3, 3], [-3, 3]),
    "row_vector": ([-3, 3], [-3, 3]),
    "col_vector": ([-3, 3], [-3, 3]),
}


def grid_points(rows, cols, origin, av, bv):
    ox, oy = origin
    ax, ay = av
    bx, by = bv
    pts = []
    pid = 1
    for r in range(rows):
        for c in range(cols):
            pts.append((pid, ox + r * ax + c * bx, oy + r * ay + c * by))
            pid += 1
    return pts


def test_exact_grid_recovers_parameters():
    pts = grid_points(3, 3, (0, 0), (2, 1), (-1, 2))
    res = reconstruct(pts, 3, 3, 0, 0, BOUNDS_WIDE)
    assert res["solvable"] is True, res.get("reason")
    p = res["parameters"]
    assert (p["origin"], p["row_vector"], p["col_vector"]) == (
        [0, 0],
        [2, 1],
        [-1, 2],
    )
    assert p["determinant"] == 5
    obj = res["objective"]
    assert (
        obj["discarded_count"],
        obj["max_manhattan_residual"],
        obj["total_manhattan_residual"],
    ) == (0, 0, 0)
    adopted = [a for a in res["assignments"] if a["adopted"]]
    cells = [(a["row"], a["col"]) for a in adopted]
    assert len(cells) == len(set(cells)) == 9


def test_missing_markers_and_scratch_outliers():
    # 4x4，漏读 4 格 + 2 划痕亮点 + 抖动
    pts, _ = __import__("scripts.smoke", fromlist=["build_case"]).build_case()
    res = reconstruct(pts, 4, 4, 1, 2, BOUNDS_WIDE)
    assert res["solvable"] is True, res.get("reason")
    p = res["parameters"]
    assert p["origin"] == [0, 0]
    assert p["row_vector"] == [3, 0]
    assert p["col_vector"] == [0, 3]
    obj = res["objective"]
    assert (
        obj["discarded_count"],
        obj["max_manhattan_residual"],
        obj["total_manhattan_residual"],
    ) == (2, 1, 3)
    discarded = {a["id"] for a in res["assignments"] if not a["adopted"]}
    assert discarded == {90, 91}
    adopted = [a for a in res["assignments"] if a["adopted"]]
    assert len(adopted) == 12
    cells = [(a["row"], a["col"]) for a in adopted]
    assert len(set(cells)) == 12
    for d in res["discarded"]:
        assert d["cells_within_tolerance"] == []
        assert d["nearest_inf_residual"] > 1


def test_unsolvable_when_points_far_away():
    pts = [(i, 100 + 3 * i, 200 + 3 * i) for i in range(1, 8)]
    res = reconstruct(pts, 3, 3, 0, 2, BOUNDS_WIDE)
    assert res["solvable"] is False
    assert "容差" in res["reason"]


def test_unsolvable_with_tight_origin_bounds():
    pts = grid_points(3, 3, (5, 5), (2, 0), (0, 2))
    tight = {
        "origin": ([-1, 1], [-1, 1]),
        "row_vector": ([1, 3], [-1, 1]),
        "col_vector": ([-1, 1], [1, 3]),
    }
    res = reconstruct(pts, 3, 3, 0, 2, tight)
    assert res["solvable"] is False
    assert res["reason"]


def test_negative_determinant_basis_rejected():
    # A=(0,2), B=(2,0) 的 det=-4（错误手性）；正确解 A=(2,0),B=(0,2)
    pts = grid_points(3, 3, (0, 0), (2, 0), (0, 2))
    res = reconstruct(pts, 3, 3, 0, 0, BOUNDS_WIDE)
    assert res["solvable"] is True
    p = res["parameters"]
    assert p["row_vector"] == [2, 0]
    assert p["col_vector"] == [0, 2]
    assert p["determinant"] > 0


def test_outlier_cap_zero_forces_no_discard():
    # 7 个点：6 个恰好落在 3x3 栅格上，1 个划痕；max_outliers=0 → 无解
    pts = grid_points(3, 3, (0, 0), (2, 0), (0, 2))[:6]
    pts.append((42, 13, 13))
    res_no = reconstruct(pts, 3, 3, 0, 0, BOUNDS_WIDE)
    assert res_no["solvable"] is False
    res_yes = reconstruct(pts, 3, 3, 0, 1, BOUNDS_WIDE)
    assert res_yes["solvable"] is True
    assert res_yes["objective"]["discarded_count"] == 1
    d = res_yes["discarded"][0]
    assert d["id"] == 42
    assert d["nearest_inf_residual"] > 0


def test_no_two_markers_share_cell_under_tolerance():
    # 两个标记都落在格位 (0,0) 的容差邻域内；3x3 其余格位放精确点。
    pts = [(1, 0, 0), (2, 1, 0)]  # 第二个只能容差吸附到 (0,0) 或 (2,0)
    pid = 3
    for r in range(3):
        for c in range(3):
            if (r, c) == (0, 0):
                continue
            pts.append((pid, 2 * r, 2 * c))
            pid += 1
    res = reconstruct(pts, 3, 3, 1, 2, BOUNDS_WIDE)
    assert res["solvable"] is True, res.get("reason")
    adopted = [a for a in res["assignments"] if a["adopted"]]
    cells = [(a["row"], a["col"]) for a in adopted]
    assert len(cells) == len(set(cells))
    by_id = {a["id"]: a for a in adopted}
    # id=2 仅能与 id=1 争 (0,0) 或与精确点争 (1,0)：它只能被弃点，
    # 绝不能与任何标记共用格位
    assert res["objective"]["discarded_count"] == 1
    assert 2 not in by_id
    assert 1 in by_id and (by_id[1]["row"], by_id[1]["col"]) == (0, 0)


def test_unordered_input_and_ids_define_output_order():
    pts = grid_points(3, 4, (1, -1), (3, 0), (0, 3))
    import random

    random.Random(7).shuffle(pts)
    res = reconstruct(pts, 3, 4, 0, 0, BOUNDS_WIDE)
    assert res["solvable"] is True, res.get("reason")
    assert [a["id"] for a in res["assignments"]] == sorted(
        a["id"] for a in res["assignments"]
    )
    p = res["parameters"]
    assert (p["origin"], p["row_vector"], p["col_vector"]) == (
        [1, -1],
        [3, 0],
        [0, 3],
    )
    assert p["determinant"] == 9


def test_residual_componentwise_within_tolerance():
    pts, _ = __import__("scripts.smoke", fromlist=["build_case"]).build_case()
    res = reconstruct(pts, 4, 4, 1, 2, BOUNDS_WIDE)
    for a in res["assignments"]:
        if a["adopted"]:
            assert abs(a["residual"][0]) <= 1
            assert abs(a["residual"][1]) <= 1


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
