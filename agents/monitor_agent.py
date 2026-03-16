"""
监控 Subagent - 全链路状态数据采集

作为 @tool 暴露给主 Agent (FaultAgent) 调用。
主 Agent 调用 run_monitoring tool 时，内部创建并调用 Monitor Subagent，
由 Subagent 自主选择调用哪些监控工具（monitor_process / analyze_logs / monitor_redis / monitor_mq / monitor_database）。

Subagent 设计要点：
- 编译后的 Agent 实例在进程生命周期内缓存复用
- Prompt 从 agents/prompts/monitor_agent.txt 加载
- 工具集仅含只读监控工具，无任何危险操作
- 记忆按 fault_id 隔离，同一故障多次调用共享上下文
"""
import logging
import threading
import uuid
from pathlib import Path
from typing import Any, Optional

from langchain.agents import create_agent
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import BaseModel, Field

from config.settings import settings
from llm.model import get_sequential_llm
from middleware.audit_log import AuditLogMiddleware
from middleware.sliding_window import SlidingWindowMiddleware
from middleware.summarization import SummarizationMiddleware
from tools import get_tool_registry

logger = logging.getLogger(__name__)

# 监控 Subagent 专用工具集（只含采集类工具，无危险操作）
MONITOR_TOOLS = get_tool_registry().get_group("monitor")

# ===== 单例缓存（线程安全）=====
_agent: Any = None
_agent_lock = threading.Lock()
# 模块级 checkpointer，按 fault_id 隔离会话，支持外部清理
_checkpointer = InMemorySaver()


def _load_prompt() -> str:
    """从 agents/prompts/monitor_agent.txt 加载 System Prompt"""
    prompt_path = Path(__file__).parent / "prompts" / "monitor_agent.txt"
    return prompt_path.read_text(encoding="utf-8").strip()


def _get_agent() -> Any:
    """获取 Monitor Subagent 编译实例（进程级单例，线程安全）"""
    global _agent
    with _agent_lock:
        if _agent is None:
            logger.info("[MonitorAgent] 编译 Monitor Subagent（首次初始化）")
            middleware = [AuditLogMiddleware()]
            if settings.sliding_window_enabled:
                middleware.append(SlidingWindowMiddleware(
                    max_messages=settings.sliding_window_max_messages,
                    preserve_recent=settings.sliding_window_preserve_recent,
                    preserve_first=settings.sliding_window_preserve_first,
                ))
            elif settings.summarization_enabled:
                middleware.append(SummarizationMiddleware(
                    max_messages=settings.summarization_max_messages,
                    max_tokens=settings.summarization_max_tokens,
                    preserve_recent=settings.summarization_preserve_recent,
                ))
            _agent = create_agent(
                model=get_sequential_llm(),
                tools=MONITOR_TOOLS,
                system_prompt=_load_prompt(),
                middleware=middleware,
                checkpointer=_checkpointer,
                name="monitor_agent",
            )
        return _agent


def purge_thread(thread_id: str) -> None:
    """清理指定 fault_id 对应的 checkpointer 数据，由 FaultAgent 在会话清理时调用"""
    try:
        storage = getattr(_checkpointer, "storage", None)
        if storage is not None and isinstance(storage, dict):
            keys_to_remove = [k for k in storage if k[0] == thread_id]
            for k in keys_to_remove:
                del storage[k]
    except Exception as e:
        logger.debug(f"[MonitorAgent] 清理 checkpointer 失败: {e}")


def _get_fault_id(config: Optional[RunnableConfig]) -> str:
    """从 RunnableConfig 中提取 fault_id 作为 thread_id"""
    if config:
        configurable = config.get("configurable", {})
        fault_id = configurable.get("fault_id")
        if fault_id:
            return fault_id
    return "monitor-task-default"


class MonitorInput(BaseModel):
    """监控工具输入参数"""
    task_description: str = Field(
        description="监控任务描述，包含故障描述、服务依赖信息等，供监控 Agent 据此采集全链路状态"
    )


@tool("run_monitoring", args_schema=MonitorInput)
def run_monitoring(task_description: str, config: RunnableConfig) -> str:
    """调用监控子Agent对故障相关服务进行全链路监控采集，包括进程状态、日志分析、Redis/MQ/数据库等中间件状态。
    输入为监控任务描述（含故障描述和服务依赖信息），返回全链路监控摘要。"""
    logger.info("[MonitorAgent] 收到监控任务")

    try:
        agent = _get_agent()
        # 每次调用使用唯一 thread_id，避免因上次递归超限导致 checkpointer
        # 残留脏 tool_call_id，引发 Qwen 400 错误
        fault_id = _get_fault_id(config)
        thread_id = f"{fault_id}-mon-{uuid.uuid4().hex[:8]}"
        result = agent.invoke(
            {"messages": [HumanMessage(content=task_description)]},
            config=RunnableConfig(
                configurable={"thread_id": thread_id},
                # 5 个监控工具 × 串行调用，每步约 2 个节点（model+tool），预留 60
                recursion_limit=60,
            ),
        )
        messages = result.get("messages", [])
        return _extract_last_text(messages)
    except Exception as e:
        logger.error(f"[MonitorAgent] 监控失败: {e}")
        return f"监控采集失败: {e}"


def _extract_last_text(messages) -> str:
    """从消息列表提取最后一条 AI 文本响应"""
    for msg in reversed(messages):
        if isinstance(msg, AIMessage) and msg.content:
            if isinstance(msg.content, str):
                return msg.content
            elif isinstance(msg.content, list):
                texts = [
                    p.get("text", "") for p in msg.content
                    if isinstance(p, dict) and p.get("type") == "text"
                ]
                return "\n".join(texts)
    return "（监控 Agent 未返回有效结果）"
