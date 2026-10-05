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
2. 复原冒烟（4×4 栅格漏读 4 格 + 2 个划痕亮点 + 坐标抖动，经 HTTP 提交）；
3. 复核冒烟（规范候选接受，及不可行 / 次优 / 同优但非规范三类拒绝）；

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

### `POST /api/wafer-grids/audit`（写入对准设备前的服务端复核）

外部工具自行算出的参数与格位表，几何可行不等于遵循了全局裁决。该接口接收
现有格式的 `reconstruction_request` 与一份 `candidate`，**残差与目标值一律
由服务端重新计算**：

```json
{
  "reconstruction_request": { "..." : "与 /reconstruct 请求体完全相同" },
  "candidate": {
    "origin": [0, 0],
    "row_vector": [3, 0],
    "col_vector": [0, 3],
    "cells":     [{"id": 1, "row": 0, "col": 0}],
    "discarded": [{"id": 90}]
  }
}
```

候选须覆盖全部标记且各出现一次（格位或弃点二选一）。服务端先做结构校验，
再按现有四级裁决（弃点数 → 最大曼哈顿残差 → 残差总和 → 参数与按编号分配的
字典序）复核，并逐项检查硬约束：

- 原点/两基向量各分量位于原闭区间，且 `det(A,B) > 0`；
- 采用标记的格位位于 `rows × cols` 内且两两互异；
- 采用标记的逐分量残差 `|dx|,|dy| ≤ tolerance`（服务端计算）；
- 弃点数不超过 `max_outliers`。

**结构错误**（候选未覆盖全部标记、某标记重复/同时声明格位与弃点、声明未知
标记、内嵌请求非法）返回 **422** 及字段级 `detail`（定位到
`candidate.cells[i].id` / `candidate.discarded[i].id` 等）。

结构合法时恒返回 **200**，由响应体区分结论，**拒绝时不回显正确分配**：

| `accepted` | `reason_code` | 含义 |
|---|---|---|
| `true`  | — | 与裁决完全一致；返回服务端计算的 `objective`（弃点数/最大残差/残差和） |
| `false` | `infeasible` | 候选违反硬约束（含 `violations` 细目），或全局裁决本身无解 |
| `false` | `suboptimal` | 候选可行但目标较差 |
| `false` | `non_canonical` | 目标相同但参数或分配不是字典序规范结果 |

`violations` 取值：`origin_out_of_bounds`、`row_vector_out_of_bounds`、
`col_vector_out_of_bounds`、`nonpositive_determinant`、`cell_out_of_grid`、
`duplicate_cell`、`residual_exceeds_tolerance`、`too_many_discarded`。

设备侧只应写入 `accepted: true` 的规范复原结果；被拒候选必须按原因码区分
**不可行（infeasible）/ 次优（suboptimal）/ 同优但不规范（non_canonical）**。

## 本地开发

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
pytest -q
python scripts/smoke.py              # 直测求解器
BASE_URL=http://127.0.0.1:8000 python scripts/smoke.py   # 走 HTTP
```
