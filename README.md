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

## 本地开发

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
pytest -q
python scripts/smoke.py              # 直测求解器
BASE_URL=http://127.0.0.1:8000 python scripts/smoke.py   # 走 HTTP
```
