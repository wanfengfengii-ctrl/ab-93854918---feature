#!/bin/sh
# 一次性校验：服务健康后运行代码测试与复原冒烟，退出码即结果。
set -e

cd /app

echo "==> [1/2] 单元测试 (pytest)"
python -m pytest -q

echo "==> [2/2] 复原冒烟（漏读标记 + 划痕亮点）"
python scripts/smoke.py

echo "==> VERIFY OK"
