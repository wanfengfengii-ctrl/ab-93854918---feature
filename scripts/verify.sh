#!/bin/sh
# 一次性校验：服务健康后运行代码测试、应用构建与复原/审计冒烟，退出码即结果。
set -e

cd /app

echo "==> [1/4] 单元测试 (pytest)"
python -m pytest -q

echo "==> [2/4] 应用构建（字节码编译与应用导入）"
python -m compileall -q app scripts
python -c "from app.main import app"

echo "==> [3/4] 复原冒烟（漏读标记 + 划痕亮点）"
python scripts/smoke.py

echo "==> [4/4] 审计冒烟（成功复核 + 不可行/次优/同优非规范拒绝）"
python scripts/audit_smoke.py

echo "==> VERIFY OK"
