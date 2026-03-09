# WeOps Agent - Claude Code 项目指南

## 项目概述

智能故障处理 Agent 系统，基于 LangChain 1.2.x 构建。主 Agent 通过 tools 调用 4 个子 Agent 完成故障处理全流程。

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
# 安装依赖
pip install -r requirements.txt

# 启动 API 服务 (0.0.0.0:8080)
python3 main.py serve

# CLI 处理故障
python3 main.py handle "订单服务超时"

# 初始化知识库
python3 main.py init-kb

# 运行测试
python3 main.py test
# 或直接
pytest tests/ -v
```

## 代码规范

### LangChain 1.2.x API

```python
# Agent 创建 (不是 create_react_agent)
from langchain.agents import create_agent
agent = create_agent(model, tools, system_prompt, middleware, checkpointer, name)

# 工具定义
from langchain_core.tools import tool
from pydantic import BaseModel, Field

class MyInput(BaseModel):
    param: str = Field(description="参数说明")

@tool("tool_name", args_schema=MyInput)
def my_tool(param: str) -> str:
    """工具描述（展示给 LLM）"""
    ...

# 中间件
from langchain.agents.middleware.types import AgentMiddleware
# hooks: before_agent, after_agent, before_model, after_model, wrap_tool_call

# Checkpointer
from langgraph.checkpoint.memory import InMemorySaver  # 不是 MemorySaver
```

### 命名约定

- 类名: `PascalCase` (如 `FaultAgent`, `AuditLogMiddleware`)
- 函数: `snake_case` (如 `handle_fault`, `monitor_process`)
- 私有函数: `_snake_case` (如 `_load_prompt`, `_extract_last_text`)
- 常量: `UPPER_CASE` (如 `MONITOR_TOOLS`, `RECOVERY_TOOLS`)
- 工具名: `snake_case` 字符串 (如 `"run_monitoring"`, `"monitor_database"`)

### 文档风格

- 模块级 docstring 描述模块职责和设计要点
- 类 docstring 含 Usage 示例
- 函数 docstring 含 Args/Returns（复杂函数）
- 业务逻辑使用中文注释

### 工具模式

所有工具使用 `@tool("name", args_schema=PydanticModel)` 装饰普通函数，不继承 BaseTool。
私有辅助函数用 `@with_retry` 装饰处理重试逻辑。

### 子 Agent 工具模式

子 Agent 暴露为 `@tool`，内部调用 `create_agent` 创建的子 Agent 实例（进程级单例缓存）：
```python
@tool("run_monitoring", args_schema=MonitorInput)
def run_monitoring(task_description: str) -> str:
    """工具描述"""
    agent = _get_agent()  # 单例缓存
    result = agent.invoke({"messages": [HumanMessage(content=task_description)]}, config=...)
    return _extract_last_text(result.get("messages", []))
```

## 关键目录

| 目录 | 说明 |
|------|------|
| `agents/` | 主 Agent + 子 Agent + prompts |
| `tools/` | 监控/恢复/通知工具 (ToolRegistry 管理) |
| `middleware/` | 审计日志 + 人工确认 + 动态模型切换中间件 |
| `memory/` | 长期记忆 (ChromaDB RAG)，短期记忆由 checkpointer 自动管理 |
| `config/` | Pydantic Settings 配置 |
| `planner/` | 故障规划器 (服务推断 + 拓扑加载) |
| `data/` | 知识库 Markdown (general/scenarios/history) |
| `service_node/` | 服务拓扑定义 (Markdown) |
| `api/` | FastAPI HTTP 接口 |

## API 端点

```
POST   /api/v1/fault/handle           # 提交故障处理
POST   /api/v1/fault/continue         # 多轮对话
GET    /api/v1/fault/{id}/status       # 查询状态
POST   /api/v1/confirm                # 人工确认危险操作
GET    /api/v1/health                 # 健康检查
POST   /api/v1/knowledge/reload       # 热加载知识库
```

## Middleware

项目使用 LangChain 1.2.x `AgentMiddleware` 机制，通过 `create_agent(middleware=[...])` 注入：

| 中间件 | hook | 说明 |
|--------|------|------|
| `AuditLogMiddleware` | 全部 | 审计日志，记录 Agent/LLM/Tool 各阶段 |
| `HumanConfirmMiddleware` | `wrap_tool_call` | 危险操作人工确认 |
| `ModelSwitchMiddleware` | `before_model` | 动态切换 LLM 模型 |

### ModelSwitchMiddleware 使用示例

```python
from middleware.model_switch import ModelSwitchMiddleware, ModelRule

agent = FaultAgent(
    model_rules=[
        ModelRule(agent_name="monitor_agent", model="gpt-4o-mini"),   # 监控用低成本模型
        ModelRule(agent_name="analysis_agent", model="gpt-4o"),       # 分析用高能力模型
        ModelRule(min_call_index=5, model="gpt-4o-mini"),             # 第5次调用后降级
        ModelRule(keyword="简单", model="gpt-4o-mini"),                # 关键词匹配
        ModelRule(condition=lambda s, r: ..., model="gpt-4o"),        # 自定义条件
    ],
)
```

## 重要注意事项

- **危险工具**: `restart_service` 等需经 `HumanConfirmMiddleware` 人工确认
- **DANGEROUS_TOOLS 集合**: `{"restart_service", "kill_process", "execute_sql", "flush_redis", "purge_mq_queue"}`
- **ToolRegistry 分组**: `monitor`(5), `recovery`(2), `notification`(1), `all`(8)
- **短期记忆**: 由 `InMemorySaver` checkpointer 按 `thread_id` 自动管理，无需手工维护
- **ChromaDB**: 需要 `langchain-chroma` 包（不是 `langchain-community`）
- **SSH 工具**: 需要 `paramiko` + 有效 SSH 密钥 (`SSH_DEFAULT_KEY_PATH`)
- **环境变量**: 复制 `.env.example` 为 `.env` 并配置 `OPENAI_API_KEY`
- **RecoveryAgent 确认模式**: 通过 `set_console_confirm_mode()` 设置（CLI=True, API=False）
