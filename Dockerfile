# ============================================================
# WeOps Intelligent Fault Agent - 多阶段构建
# ============================================================
# 构建:  docker build -t weops-agent .
# 运行:  docker run --env-file .env -p 8080:8080 weops-agent
# ============================================================

# ---------- 阶段 1: 依赖安装 ----------
FROM python:3.11-slim AS builder

WORKDIR /build

# 系统依赖（编译 psycopg2-binary、paramiko 等需要）
RUN apt-get update && \
    apt-get install -y --no-install-recommends gcc libpq-dev && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# ---------- 阶段 2: 运行时镜像 ----------
FROM python:3.11-slim

# 运行时系统依赖
RUN apt-get update && \
    apt-get install -y --no-install-recommends libpq5 curl && \
    rm -rf /var/lib/apt/lists/*

# 非 root 用户
RUN groupadd -r weops && useradd -r -g weops -d /app -s /sbin/nologin weops

WORKDIR /app

# 从 builder 阶段复制已安装的 Python 包
COPY --from=builder /install /usr/local

# 复制应用代码
COPY agents/ agents/
COPY api/ api/
COPY config/ config/
COPY data/ data/
COPY llm/ llm/
COPY memory/ memory/
COPY messages/ messages/
COPY middleware/ middleware/
COPY planner/ planner/
COPY service_node/ service_node/
COPY tools/ tools/
COPY main.py .

# 创建数据目录并设置权限
RUN mkdir -p logs chroma_db && \
    chown -R weops:weops /app

USER weops

# 环境变量默认值
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    API_HOST=0.0.0.0 \
    API_PORT=8080 \
    LOG_LEVEL=INFO

EXPOSE 8080

# 健康检查
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD curl -sf http://localhost:8080/api/v1/health || exit 1

ENTRYPOINT ["python3", "main.py"]
CMD ["serve"]
