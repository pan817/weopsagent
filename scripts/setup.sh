#!/usr/bin/env bash
# WeOps Agent 一键安装脚本
# 用法: bash scripts/setup.sh
set -e

echo "========================================"
echo "  WeOps Intelligent Fault Agent 安装"
echo "========================================"

# 检查 Python 版本
PYTHON_VERSION=$(python3 --version 2>&1 | grep -oP '\d+\.\d+' | head -1)
REQUIRED_VERSION="3.10"

if python3 -c "import sys; exit(0 if sys.version_info >= (3, 10) else 1)" 2>/dev/null; then
    echo "✅ Python 版本: $(python3 --version)"
else
    echo "❌ 需要 Python 3.10 或更高版本，当前版本: $PYTHON_VERSION"
    exit 1
fi

# 创建虚拟环境
if [ ! -d ".venv" ]; then
    echo ""
    echo "📦 创建虚拟环境..."
    python3 -m venv .venv
fi

# 激活虚拟环境
source .venv/bin/activate
echo "✅ 虚拟环境已激活"

# 升级 pip
pip install --upgrade pip -q

# 安装依赖
echo ""
echo "📦 安装依赖包（可能需要几分钟）..."
pip install -r requirements.txt

echo ""
echo "✅ 依赖安装完成"

# 检查 .env 文件
if [ ! -f ".env" ]; then
    echo ""
    echo "📝 创建 .env 配置文件..."
    cp .env.example .env
    echo "⚠️  请编辑 .env 文件，填入你的 OpenAI API Key 和其他配置"
fi

# 创建必要目录
echo ""
echo "📁 创建必要目录..."
mkdir -p logs chroma_db

# 初始化知识库
echo ""
echo "📚 初始化知识库..."
python3 scripts/init_knowledge_base.py

echo ""
echo "========================================"
echo "✅ 安装完成！"
echo ""
echo "下一步操作："
echo "1. 编辑 .env 文件，填入 OPENAI_API_KEY 等配置"
echo "2. 启动服务: bash scripts/start.sh"
echo "3. 运行测试: bash scripts/run_tests.sh"
echo "========================================"
