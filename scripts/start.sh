#!/usr/bin/env bash
# WeOps Agent 服务启动脚本
# 用法: bash scripts/start.sh [--port 8080] [--host 0.0.0.0]
set -e

# 默认配置
HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8080}"
WORKERS="${WORKERS:-1}"
LOG_LEVEL="${LOG_LEVEL:-info}"

# 解析参数
while [[ $# -gt 0 ]]; do
    case $1 in
        --port) PORT="$2"; shift 2 ;;
        --host) HOST="$2"; shift 2 ;;
        --workers) WORKERS="$2"; shift 2 ;;
        --log-level) LOG_LEVEL="$2"; shift 2 ;;
        --dev) DEV_MODE=true; shift ;;
        *) echo "未知参数: $1"; exit 1 ;;
    esac
done

echo "========================================"
echo "  启动 WeOps Intelligent Fault Agent"
echo "  地址: http://${HOST}:${PORT}"
echo "========================================"

# 激活虚拟环境（如存在）
if [ -d ".venv" ]; then
    source .venv/bin/activate
fi

# 检查配置文件
if [ ! -f ".env" ]; then
    echo "⚠️  未找到 .env 文件，使用默认配置"
    echo "   请参考 .env.example 创建配置文件"
fi

# 创建日志目录
mkdir -p logs

# 启动服务
if [ "$DEV_MODE" = "true" ]; then
    echo "🔧 开发模式启动（热重载）..."
    uvicorn api.server:app \
        --host "$HOST" \
        --port "$PORT" \
        --reload \
        --log-level "$LOG_LEVEL"
else
    echo "🚀 生产模式启动..."
    uvicorn api.server:app \
        --host "$HOST" \
        --port "$PORT" \
        --workers "$WORKERS" \
        --log-level "$LOG_LEVEL" \
        --access-log \
        --log-config scripts/log_config.yaml 2>/dev/null || \
    uvicorn api.server:app \
        --host "$HOST" \
        --port "$PORT" \
        --workers "$WORKERS" \
        --log-level "$LOG_LEVEL"
fi
