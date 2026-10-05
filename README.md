# Wafer Grid Reconstruction Service

从无序、带唯一编号的整数标记坐标中恢复晶圆栅格：联合选择**原点 O、行基向量 A、
列基向量 B**以及标记到互异格位的分配，容忍漏读（缺标记）与最多两个杂点
（划痕亮点）。

## 优化目标（字典序）

对每组候选参数与分配依次最小化：

1. **弃点数**（≤ `max_outliers`）；
2. **最大曼哈顿残差**；
3. **曼哈顿残差总和**；
4. **完整参数与按编号排列的分配序列**（枚举序下的首个最优，保证确定性）。

硬约束：

- `det(A, B) > 0`；
- 采用点预测坐标逐分量满足 `|dx| ≤ tolerance 且 |dy| ≤ tolerance`；
- 任意两个标记不得占用同一格位（二分图匹配保证）；
- O / A / B 各分量取自调用方给定的、跨度 ≤ 6 的闭区间。

算法：枚举至多 `7^6` 组整数参数（包围盒 + 邻域集合两级预过滤，det 过滤），
Kuhn 求最大匹配与瓶颈残差，最小费用流求残差和，再逐点贪心 + 后缀可行性检查
得到字典序最小分配。

## 运行

```bash
# 端口可配置（默认 8000）
API_PORT=9000 ./verify
```

`verify` 会构建镜像、启动 `web` 服务（带容器健康检查），待服务健康后由一次性
`verify` 容器执行：

1. `pytest` 代码测试；
2. 应用构建（字节码编译 + 应用导入检查）；
3. 复原冒烟（4×4 栅格漏读 4 格 + 2 个划痕亮点 + 坐标抖动，经 HTTP 提交）；
4. 审计冒烟（成功复核 + 不可行/次优/同优非规范三类拒绝 + 结构错误 422）；

并以自身退出码汇报（成功 0）。单独启动服务：`API_PORT=9000 docker compose up web`。

## API

`GET /health` → `{"status":"healthy"}`

`POST /api/wafer-grids/reconstruct`：

```json
{
  "points": [{"id": 1, "x": 0, "y": 0}],
  "rows": 4,
  "cols": 4,
  "max_outliers": 2,
  "tolerance": 1,
  "origin_bounds":      {"x": {"lo": -3, "hi": 3}, "y": {"lo": -3, "hi": 3}},
  "row_vector_bounds":  {"x": {"lo": -3, "hi": 3}, "y": {"lo": -3, "hi": 3}},
  "col_vector_bounds":  {"x": {"lo": -3, "hi": 3}, "y": {"lo": -3, "hi": 3}}
}
```

约束：7–14 个唯一编号点；行/列 3–7；`max_outliers` 0–2；各区间跨度 ≤ 6。

成功返回（HTTP 200，`solvable: true`）：`parameters`（原点、两基向量、行列式）、
`objective`（弃点数 / 最大残差 / 残差和）、`assignments`（逐点格位、预测坐标、
残差）、`discarded`（弃点证据：最近格位、最近残差、容差内候选、弃点原因）。

几何上无解时返回 HTTP 200、`solvable: false` 及明确的中文 `reason`
（建议放宽容差/区间或提高弃点上限）；请求本身不合法（编号重复、点数越界、
区间跨度超 6 等）返回 HTTP 422 并附字段级错误。

`POST /api/wafer-grids/audit`：复原结果写入对准设备前的服务端复核。接收
`reconstruction_request`（同重建请求）与 `candidate`（原点、两条基向量、
每个标记的格位或弃点声明）；残差与目标值由服务端计算，候选不提交：

```json
{
  "reconstruction_request": { "...": "同 /api/wafer-grids/reconstruct 请求体" },
  "candidate": {
    "origin": [0, 0],
    "row_vector": [3, 0],
    "col_vector": [0, 3],
    "assignments": [
      {"id": 1, "row": 0, "col": 0},
      {"id": 90, "discarded": true}
    ]
  }
}
```

复核流程：候选须覆盖全部标记且各出现一次（否则 422 字段级错误）；随后逐项
校验参数位于原区间、行列式为正、采用格位有效且互异、逐分量残差不越过容差、
弃点数不超过原上限；最后按现有四级裁决重求规范解比对。一致时返回
`{"accepted": true, "objective": {...}}`（目标值由服务端计算）；否则返回
HTTP 200、`accepted: false` 及稳定原因码，**不回显正确分配**：

| category         | reason_code                   | 含义                       |
|------------------|-------------------------------|----------------------------|
| `infeasible`     | `PARAMETERS_OUT_OF_BOUNDS`    | 参数超出原区间             |
| `infeasible`     | `NON_POSITIVE_DETERMINANT`    | 行列式非正                 |
| `infeasible`     | `CELL_OUT_OF_RANGE`           | 格位越出栅格               |
| `infeasible`     | `DUPLICATE_CELL`              | 两标记占用同一格位         |
| `infeasible`     | `RESIDUAL_EXCEEDS_TOLERANCE`  | 逐分量残差越过容差         |
| `infeasible`     | `DISCARD_LIMIT_EXCEEDED`      | 弃点数超过原上限           |
| `suboptimal`     | `SUBOPTIMAL_OBJECTIVE`        | 几何可行但目标较差         |
| `non_canonical`  | `NON_CANONICAL`               | 目标相同但参数/分配非规范  |

设备侧只能写入 `accepted: true` 的规范复原结果；被拒候选可按 `category`
明确区分不可行、次优与同优但不规范。

## 本地开发

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
pytest -q
python scripts/smoke.py              # 直测求解器
python scripts/audit_smoke.py        # 直测审计端点（TestClient）
BASE_URL=http://127.0.0.1:8000 python scripts/smoke.py        # 走 HTTP
BASE_URL=http://127.0.0.1:8000 python scripts/audit_smoke.py  # 走 HTTP
```
