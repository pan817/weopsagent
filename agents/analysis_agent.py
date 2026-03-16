"""
分析 Subagent - 故障根因分析与恢复方案制定

作为 @tool 暴露给主 Agent (FaultAgent) 调用。
主 Agent 调用 run_analysis tool 时，内部创建并调用 Analysis Subagent，
由 Subagent 基于监控数据和知识库进行纯 LLM 推理，输出根因分析和恢复方案。

Subagent 设计要点：
- 不绑定任何工具（纯 LLM 推理）
- Prompt 从 agents/prompts/analysis_agent.txt 加载
- 通过 Prompt 引导 LLM 输出结构化 Markdown
- 记忆按 fault_id 隔离，同一故障多次调用共享上下文
"""
import logging
import re
import threading
from pathlib import Path
from typing import Any, Optional

from langchain.agents import create_agent
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import BaseModel, Field

from config.settings import settings
from llm.model import get_llm
from middleware.audit_log import AuditLogMiddleware
from middleware.sliding_window import SlidingWindowMiddleware
from middleware.summarization import SummarizationMiddleware

logger = logging.getLogger(__name__)

# ===== 单例缓存（线程安全）=====
_agent: Any = None
_agent_lock = threading.Lock()
# 模块级 checkpointer，按 fault_id 隔离会话，支持外部清理
_checkpointer = InMemorySaver()


def _load_prompt() -> str:
    """从 agents/prompts/analysis_agent.txt 加载 System Prompt"""
    prompt_path = Path(__file__).parent / "prompts" / "analysis_agent.txt"
    return prompt_path.read_text(encoding="utf-8").strip()


def _get_agent() -> Any:
    """获取 Analysis Subagent 编译实例（进程级单例，线程安全）"""
    global _agent
    with _agent_lock:
        if _agent is None:
            logger.info("[AnalysisAgent] 编译 Analysis Subagent（首次初始化）")
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
                model=get_llm(),
                tools=[],
                system_prompt=_load_prompt(),
                middleware=middleware,
                checkpointer=_checkpointer,
                name="analysis_agent",
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
        logger.debug(f"[AnalysisAgent] 清理 checkpointer 失败: {e}")


def _get_fault_id(config: Optional[RunnableConfig]) -> str:
    """从 RunnableConfig 中提取 fault_id 作为 thread_id"""
    if config:
        configurable = config.get("configurable", {})
        fault_id = configurable.get("fault_id")
        if fault_id:
            return fault_id
    return "analysis-task-default"


class AnalysisInput(BaseModel):
    """分析工具输入参数"""
    task_description: str = Field(
        description="分析任务描述，包含故障描述、监控摘要、知识库参考等，供分析 Agent 进行根因分析"
    )


@tool("run_analysis", args_schema=AnalysisInput)
def run_analysis(task_description: str, config: RunnableConfig) -> str:
    """调用分析子Agent进行故障根因分析和恢复方案制定。
    输入为分析任务描述（含故障描述、监控摘要、知识库参考），返回包含根因分析和恢复方案的结构化报告。"""
    logger.info("[AnalysisAgent] 收到分析任务")

    try:
        agent = _get_agent()
        # 使用 fault_id 作为 thread_id，同一故障多次调用共享上下文
        thread_id = _get_fault_id(config)
        result = agent.invoke(
            {"messages": [HumanMessage(content=task_description)]},
            config=RunnableConfig(
                configurable={"thread_id": thread_id},
            ),
        )
        messages = result.get("messages", [])
        return _extract_last_text(messages)
    except Exception as e:
        logger.error(f"[AnalysisAgent] 分析失败: {e}")
        return f"分析失败: {e}"


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
    return "（分析 Agent 未返回有效结果）"


def _extract_section(text: str, section_name: str) -> str:
    """从 Markdown 格式文本中提取指定 ### 小节的内容。"""
    pattern_exact = rf"###\s+{re.escape(section_name)}\s*\n(.*?)(?=\n##|\Z)"
    match = re.search(pattern_exact, text, re.DOTALL)
    if match:
        return match.group(1).strip()

    pattern_loose = rf"###\s+{re.escape(section_name)}[^\n（(]*\n(.*?)(?=\n##|\Z)"
    match = re.search(pattern_loose, text, re.DOTALL)
    if match:
        return match.group(1).strip()

    return ""
