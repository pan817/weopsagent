# WeOps Agent - Claude Code 项目指南

## 架构

```
FaultAgent (主Agent, create_agent)
├── run_monitoring   → MonitorAgent (5个监控工具: process/redis/mq/database/logs)
├── run_analysis     → AnalysisAgent (纯LLM推理, 无工具)
├── run_recovery     → RecoveryAgent (restart_service + store_knowledge, 含人工确认)
└── run_notification → NotificationAgent (send_notification, 支持钉钉/Slack/Email)
```

## 常用命令

```bash
pip install -r requirements.txt
python3 main.py serve          # API服务 0.0.0.0:8080
python3 main.py handle "故障描述"
python3 main.py test / pytest tests/ -v
```

## 关键目录

| 目录 | 说明 |
|------|------|
| `agents/` | 主 Agent + 子 Agent + prompts/ |
| `tools/` | 监控/恢复/通知工具 (ToolRegistry 管理) |
| `tools/mcp/` | MCP 集成 (Prometheus/ES/K8s/DingTalk/PostgreSQL) |
| `middleware/` | audit_log / human_confirm / model_switch / rate_limit / sliding_window / summarization |
| `memory/` | 长期记忆 ChromaDB，短期记忆由 checkpointer 自动管理 |
| `planner/` | 故障规划器 (服务推断 + 拓扑) |
| `api/` | FastAPI HTTP 接口 |

## API 端点

```
POST /api/v1/fault/handle      POST /api/v1/fault/continue
GET  /api/v1/fault/{id}/status POST /api/v1/confirm
GET  /api/v1/health            POST /api/v1/knowledge/reload
```

## LangChain 1.2.x 关键 API

```python
# Agent 创建（非 create_react_agent）
from langchain.agents import create_agent
agent = create_agent(model, tools, system_prompt, middleware=[], checkpointer=..., name=...)

# 工具定义
from langchain_core.tools import tool
@tool("tool_name", args_schema=MyPydanticModel)
def my_tool(param: str) -> str: ...

# 中间件
from langchain.agents.middleware.types import AgentMiddleware
# hooks: before_agent / after_agent / before_model / after_model / wrap_tool_call(request, handler)

# Checkpointer（注意类名）
from langgraph.checkpoint.memory import InMemorySaver  # 不是 MemorySaver
```

## 代码规范

- 类: `PascalCase`，函数: `snake_case`，私有: `_snake_case`，常量: `UPPER_CASE`，工具名: `"snake_case"` 字符串
- 所有工具用 `@tool("name", args_schema=Model)` 装饰普通函数，不继承 BaseTool
- 子 Agent 暴露为 `@tool`，内部用进程级单例缓存 Agent 实例（`_get_agent()`）
- 辅助函数用 `@with_retry` 处理重试；业务逻辑用中文注释

## Middleware

通过 `create_agent(middleware=[...])` 注入：

| 中间件 | hook | 说明 |
|--------|------|------|
| `AuditLogMiddleware` | 全部 | 审计日志 |
| `HumanConfirmMiddleware` | `wrap_tool_call` | 危险操作确认 |
| `ModelSwitchMiddleware` | `before_model` | 动态切换模型（见 middleware/model_switch.py） |
| `RateLimitMiddleware` | `before_model` + `wrap_tool_call` | 限流（env: RATE_LIMIT_MODEL_RPM 等） |
| `SlidingWindowMiddleware` | `before_model` | 滑动窗口裁剪，默认启用，零 LLM 开销 |
| `SummarizationMiddleware` | `before_model` | LLM 压缩历史，与 SlidingWindow 二选一 |

## 重要注意事项

- **DANGEROUS_TOOLS**: `{"restart_service", "kill_process", "execute_sql", "flush_redis", "purge_mq_queue", "k8s_restart_deployment"}` → 需 HumanConfirmMiddleware
- **ToolRegistry 分组**: `monitor`(5+MCP), `recovery`(2+MCP), `notification`(1+MCP), `all`
- **ChromaDB**: 需 `langchain-chroma`（非 `langchain-community`）
- **RecoveryAgent 确认模式**: `set_console_confirm_mode()`（CLI=True, API=False）
- **环境变量**: 复制 `.env.example` → `.env`，配置 `OPENAI_API_KEY`

## MCP 集成

| MCP Server | 工具 | 启用条件 |
|-----------|------|---------|
| Prometheus | `query_prometheus`, `query_prometheus_range` | `MCP_PROMETHEUS_URL` |
| Elasticsearch | `search_logs`, `aggregate_logs` | `MCP_ELASTICSEARCH_URL` |
| Kubernetes | `k8s_get_pods`, `k8s_get_pod_logs`, `k8s_restart_deployment`⚠️, `k8s_describe_resource` | `MCP_KUBERNETES_ENABLED=true` |
| DingTalk | `dingtalk_send_text`, `dingtalk_send_markdown` | `MCP_DINGTALK_WEBHOOK` |
| PostgreSQL | `pg_query`, `pg_slow_queries`, `pg_table_info` | `MCP_POSTGRES_DSN` |
