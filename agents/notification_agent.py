"""
通知 Subagent - 故障告警与恢复通知发送

作为 @tool 暴露给主 Agent (FaultAgent) 调用。
主 Agent 调用 run_notification tool 时，内部创建并调用 Notification Subagent，
由 Subagent 根据故障状态发送相应通知（DingTalk / Slack / Email）。

Subagent 设计要点：
- 编译后的 Agent 实例在进程生命周期内缓存复用
- Prompt 从 agents/prompts/notification_agent.txt 加载
- 根据通知内容自动选择通知类型（告警/恢复/升级）

使用工具：send_notification
"""
import logging
from pathlib import Path
from typing import Any

from langchain.agents import create_agent
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import BaseModel, Field

from llm.model import get_llm
from middleware.audit_log import AuditLogMiddleware
from tools import get_tool_registry

logger = logging.getLogger(__name__)

NOTIFY_TOOLS = get_tool_registry().get_group("notification")

# ===== 单例缓存 =====
_agent: Any = None


def _load_prompt() -> str:
    """从 agents/prompts/notification_agent.txt 加载 System Prompt"""
    prompt_path = Path(__file__).parent / "prompts" / "notification_agent.txt"
    return prompt_path.read_text(encoding="utf-8").strip()


def _get_agent() -> Any:
    """获取 Notification Subagent 编译实例（进程级单例）"""
    global _agent
    if _agent is None:
        logger.info("[NotificationAgent] 编译 Notification Subagent（首次初始化）")
        _agent = create_agent(
            model=get_llm(),
            tools=NOTIFY_TOOLS,
            system_prompt=_load_prompt(),
            middleware=[AuditLogMiddleware()],
            checkpointer=InMemorySaver(),
            name="notification_agent",
        )
    return _agent


class NotificationInput(BaseModel):
    """通知工具输入参数"""
    task_description: str = Field(
        description="通知任务描述，包含故障信息、处理状态、根因分析等，供通知 Agent 发送相应通知"
    )


@tool("run_notification", args_schema=NotificationInput)
def run_notification(task_description: str) -> str:
    """调用通知子Agent发送故障相关通知，支持DingTalk/Slack/Email等渠道。
    输入为通知任务描述（含故障信息和处理状态），返回通知发送结果。"""
    logger.info("[NotificationAgent] 收到通知任务")

    try:
        agent = _get_agent()
        result = agent.invoke(
            {"messages": [HumanMessage(content=task_description)]},
            config=RunnableConfig(
                configurable={"thread_id": "notify-task"},
            ),
        )
        messages = result.get("messages", [])
        return _extract_last_text(messages)
    except Exception as e:
        logger.error(f"[NotificationAgent] 通知发送失败: {e}")
        return f"通知发送失败: {e}"


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
    return "（通知 Agent 未返回有效结果）"
