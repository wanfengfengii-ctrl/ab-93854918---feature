"""复原冒烟：含漏读标记与划痕亮点（杂点）的栅格复原。

可直接运行（不依赖服务）；verify 流程在服务健康后通过 BASE_URL 走 HTTP，
同时保留对核心算法的直测。用法::

    python scripts/smoke.py            # 直测求解器
    BASE_URL=http://web:8000 python scripts/smoke.py   # 走 HTTP
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.solver import reconstruct  # noqa: E402

BOUNDS = {
    "origin": ([-3, 3], [-3, 3]),
    "row_vector": ([-3, 3], [-3, 3]),
    "col_vector": ([-3, 3], [-3, 3]),
}


def build_case():
    """4x4 栅格，O=(0,0)，行向量 A=(3,0)，列向量 B=(0,3)，det=9。

    故意漏读 3 个格位，并混入 2 个划痕亮点；再给 3 个标记施加分量 ≤1 的扰动。
    """
    true_cells = {
        (r, c): (3 * r, 3 * c) for r in range(4) for c in range(4)
    }
    missing = {(1, 1), (2, 3), (3, 0), (0, 3)}  # 漏读 4 格
    # 抖动均为曼哈顿 1：任何其他基向量若最大残差同为 1，也必须在众多
    # 精确点上付出更大残差和，真栅格凭第三级目标（残差总和）唯一胜出
    jitter = {(0, 2): (1, 0), (2, 1): (0, -1), (3, 3): (-1, 0)}
    points = []
    pid = 1
    for r in range(4):
        for c in range(4):
            if (r, c) in missing:
                continue
            x, y = true_cells[(r, c)]
            if (r, c) in jitter:
                dx, dy = jitter[(r, c)]
                x += dx
                y += dy
            points.append((pid, x, y))
            pid += 1
    # 两个划痕亮点（杂点），故意打散顺序
    points.extend(
        [
            (90, 17, -5),
            (91, -8, 14),
        ]
    )
    # 打乱坐标顺序，验证“无序坐标恢复”
    import random

    random.Random(42).shuffle(points)
    return points, missing


def expected_payload(points):
    return {
        "points": [{"id": i, "x": x, "y": y} for i, x, y in points],
        "rows": 4,
        "cols": 4,
        "max_outliers": 2,
        "tolerance": 1,
        "origin_bounds": {"x": {"lo": -3, "hi": 3}, "y": {"lo": -3, "hi": 3}},
        "row_vector_bounds": {"x": {"lo": -3, "hi": 3}, "y": {"lo": -3, "hi": 3}},
        "col_vector_bounds": {"x": {"lo": -3, "hi": 3}, "y": {"lo": -3, "hi": 3}},
    }


def check_result(result):
    assert result["solvable"] is True, result.get("reason")
    p = result["parameters"]
    assert p["origin"] == [0, 0], p
    assert p["row_vector"] == [3, 0], p
    assert p["col_vector"] == [0, 3], p
    assert p["determinant"] == 9

    obj = result["objective"]
    assert obj["discarded_count"] == 2, obj
    assert obj["max_manhattan_residual"] == 1, obj
    assert obj["total_manhattan_residual"] == 3, obj  # 三个抖动点各 1

    adopted = [a for a in result["assignments"] if a["adopted"]]
    discarded = [a for a in result["assignments"] if not a["adopted"]]
    assert len(adopted) == 12
    assert len(discarded) == 2
    assert {a["id"] for a in discarded} == {90, 91}

    # 每个被采用标记落回真实格位
    true_xy_to_rc = {(3 * r, 3 * c): (r, c) for r in range(4) for c in range(4)}
    cells = set()
    for a in adopted:
        assert max(abs(a["residual"][0]), abs(a["residual"][1])) <= 1
        pr, pc = true_xy_to_rc[tuple(a["predicted"])]
        assert (a["row"], a["col"]) == (pr, pc)
        cells.add((a["row"], a["col"]))
    assert len(cells) == len(adopted), "两个标记占用了同一格位"

    # 弃点证据
    for d in result["discarded"]:
        assert d["id"] in (90, 91)
        assert d["nearest_cell"] is not None
        assert d["nearest_inf_residual"] > 1
        assert d["cells_within_tolerance"] == []
    print(
        f"  弃点 {[d['id'] for d in result['discarded']]}，"
        f"目标 = (k={obj['discarded_count']}, "
        f"max={obj['max_manhattan_residual']}, sum={obj['total_manhattan_residual']})"
    )


def main():
    points, _ = build_case()
    base_url = os.environ.get("BASE_URL")
    if base_url:
        import urllib.request

        payload = expected_payload(points)
        req = urllib.request.Request(
            base_url.rstrip("/") + "/api/wafer-grids/reconstruct",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read())
        print(f"==> 通过 HTTP ({base_url}) 冒烟")
    else:
        result = reconstruct(points, 4, 4, 1, 2, BOUNDS)
        print("==> 直测求解器冒烟")
    check_result(result)
    print("==> 冒烟通过：漏读 4 格 + 2 划痕亮点均正确处理")


if __name__ == "__main__":
    main()
