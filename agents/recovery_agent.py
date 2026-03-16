"""
恢复 Subagent - 故障恢复操作执行

作为 @tool 暴露给主 Agent (FaultAgent) 调用。
主 Agent 调用 run_recovery tool 时，内部创建并调用 Recovery Subagent，
由 Subagent 根据分析结果执行恢复操作（restart_service、store_knowledge）。

Subagent 设计要点：
- 按 console_confirm_mode 分别缓存（最多 2 个实例）
- HumanConfirmMiddleware 在危险工具调用前自动触发人工确认
- Prompt 从 agents/prompts/recovery_agent.txt 加载
- 记忆按 fault_id 隔离，同一故障多次调用共享上下文

使用工具：restart_service（⚠️危险）、store_knowledge（安全）
"""
import logging
import re
import threading
from pathlib import Path
from typing import Any, Dict, Optional

from langchain.agents import create_agent
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import BaseModel, Field

from config.settings import settings
from llm.model import get_llm
from middleware.audit_log import AuditLogMiddleware
from middleware.human_confirm import HumanConfirmMiddleware
from middleware.sliding_window import SlidingWindowMiddleware
from middleware.summarization import SummarizationMiddleware
from tools import get_tool_registry

logger = logging.getLogger(__name__)

# 恢复 Subagent 工具集（含危险工具，需人工确认中间件把关）
RECOVERY_TOOLS = get_tool_registry().get_group("recovery")

# ===== 单例缓存（按 console_confirm_mode 分别缓存，线程安全）=====
_agent_cache: Dict[bool, Any] = {}
_cache_lock = threading.Lock()
# 模块级 checkpointer，按 fault_id 隔离会话，支持外部清理
# 所有 confirm_mode 的 agent 共享同一个 checkpointer
_checkpointer = InMemorySaver()

# 当前 console_confirm_mode 设置（由 FaultAgent 初始化时设置）
_console_confirm_mode: bool = True
_mode_lock = threading.Lock()


def set_console_confirm_mode(mode: bool) -> None:
    """设置 RecoveryAgent 的 console_confirm_mode（由 FaultAgent 在初始化时调用）"""
    global _console_confirm_mode
    with _mode_lock:
        _console_confirm_mode = mode


def _load_prompt() -> str:
    """从 agents/prompts/recovery_agent.txt 加载 System Prompt"""
    prompt_path = Path(__file__).parent / "prompts" / "recovery_agent.txt"
    return prompt_path.read_text(encoding="utf-8").strip()


def _get_agent(console_confirm_mode: bool = True) -> Any:
    """获取 Recovery Subagent 编译实例（按确认模式分别缓存，线程安全）"""
    with _cache_lock:
        if console_confirm_mode not in _agent_cache:
            logger.info(
                f"[RecoveryAgent] 编译 Recovery Subagent "
                f"console_confirm_mode={console_confirm_mode}（首次初始化）"
            )
            middleware = [AuditLogMiddleware()]
            if settings.human_confirm_enabled:
                middleware.append(HumanConfirmMiddleware(console_mode=console_confirm_mode))
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
            _agent_cache[console_confirm_mode] = create_agent(
                model=get_llm(),
                tools=RECOVERY_TOOLS,
                system_prompt=_load_prompt(),
                middleware=middleware,
                checkpointer=_checkpointer,
                name="recovery_agent",
            )
        return _agent_cache[console_confirm_mode]


def purge_thread(thread_id: str) -> None:
    """清理指定 fault_id 对应的 checkpointer 数据，由 FaultAgent 在会话清理时调用"""
    try:
        storage = getattr(_checkpointer, "storage", None)
        if storage is not None and isinstance(storage, dict):
            keys_to_remove = [k for k in storage if k[0] == thread_id]
            for k in keys_to_remove:
                del storage[k]
    except Exception as e:
        logger.debug(f"[RecoveryAgent] 清理 checkpointer 失败: {e}")


def _get_fault_id(config: Optional[RunnableConfig]) -> str:
    """从 RunnableConfig 中提取 fault_id 作为 thread_id"""
    if config:
        configurable = config.get("configurable", {})
        fault_id = configurable.get("fault_id")
        if fault_id:
            return fault_id
    return "recovery-task-default"


class RecoveryInput(BaseModel):
    """恢复工具输入参数"""
    task_description: str = Field(
        description="恢复任务描述，包含故障信息、根因分析、恢复方案等，供恢复 Agent 执行恢复操作"
    )


@tool("run_recovery", args_schema=RecoveryInput)
def run_recovery(task_description: str, config: RunnableConfig) -> str:
    """调用恢复子Agent执行故障恢复操作，包括服务重启（需人工确认）和知识存储。
    输入为恢复任务描述（含故障信息、根因分析、恢复方案），返回恢复操作结果和状态（RESOLVED/PARTIAL/FAILED）。"""
    logger.info("[RecoveryAgent] 收到恢复任务")

    try:
        with _mode_lock:
            mode = _console_confirm_mode
        agent = _get_agent(mode)
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
    except PermissionError as e:
        logger.warning(f"[RecoveryAgent] 危险操作被拒绝: {e}")
        return f"操作被拒绝：{e}"
    except Exception as e:
        logger.error(f"[RecoveryAgent] 恢复失败: {e}")
        return f"恢复执行失败: {e}"


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
    return "（恢复 Agent 未返回有效结果）"


def _check_resolved(recovery_text: str) -> bool:
    """
    从 RecoveryAgent 输出文本中判断故障是否已恢复。

    解析策略：
    1. 结构化标记：匹配 `RECOVERY_STATUS: RESOLVED/PARTIAL/FAILED`
    2. 关键词匹配（降级）
    """
    status_match = re.search(
        r"RECOVERY_STATUS\s*:\s*(RESOLVED|PARTIAL|FAILED)",
        recovery_text,
        re.IGNORECASE,
    )
    if status_match:
        return status_match.group(1).upper() == "RESOLVED"

    text_lower = recovery_text.lower()
    if any(kw in text_lower for kw in ["partial", "failed", "未恢复", "恢复失败", "需人工"]):
        return False
    resolved_keywords = ["resolved", "已恢复", "恢复成功", "故障已消除"]
    for kw in resolved_keywords:
        if kw in text_lower:
            negative_prefix = any(
                neg + kw in text_lower
                for neg in ["未", "没有", "尚未", "仍未"]
            )
            if not negative_prefix:
                return True
    return False
