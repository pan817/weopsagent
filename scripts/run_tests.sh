#!/usr/bin/env bash
# 运行测试脚本
set -e

echo "========================================"
echo "  运行 WeOps Agent 测试套件"
echo "========================================"

# 激活虚拟环境
if [ -d ".venv" ]; then
    source .venv/bin/activate
fi

# 运行测试
echo ""
echo "🧪 运行所有测试..."
python3 -m pytest tests/ \
    -v \
    --tb=short \
    --color=yes \
    -x \
    "$@"

echo ""
echo "✅ 测试完成"
