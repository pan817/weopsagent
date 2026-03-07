"""
告警 Agent - 专注发送通知

职责：
- 在故障发生时发送告警通知（故障开始通知）
- 在故障恢复时发送恢复通知
- 汇总完整故障处理报告发送给相关人员

使用工具：notification（支持 DingTalk/Slack/Email）
"""
import logging
from typing import Any, Dict

from langchain.agents import create_agent
from langchain_core.messages import HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver

from llm.model import get_llm
from middleware.audit_log import AuditLogMiddleware
from tools.notification import NotificationTool

logger = logging.getLogger(__name__)

NOTIFY_TOOLS = [NotificationTool()]

_SYSTEM_PROMPT = """你是专业的 WeOps 告警 Agent，负责在适当时机发送通知。

## 你的职责
1. **故障通知**：向相关人员发送故障发生通知，包含故障描述、影响范围、处理状态
2. **恢复通知**：故障恢复后发送恢复通知，包含恢复时间、处理方式
3. **报告汇总**：生成完整的故障处理报告并发送

## 通知内容要求
通知内容应包含：
- 故障 ID 和服务名称
- 故障现象和影响范围
- 根因分析结论（简洁）
- 已采取的处理措施
- 当前状态（处理中 / 已恢复 / 需人工介入）
- 故障持续时间（如已知）

## 原则
- 发送后确认通知成功
- 如发送失败，记录错误并尝试备用渠道"""


def create_notification_agent(fault_id: str = None):
    """创建告警 Agent"""
    return create_agent(
        model=get_llm(),
        tools=NOTIFY_TOOLS,
        system_prompt=_SYSTEM_PROMPT,
        middleware=[AuditLogMiddleware(fault_id=fault_id)],
        checkpointer=InMemorySaver(),
        name="notification_agent",
    )


def run_notification_node(state: Dict[str, Any], config: RunnableConfig) -> Dict[str, Any]:
    """
    告警节点执行函数（LangGraph 节点）

    根据 is_resolved 状态发送对应通知（告警通知或恢复通知），
    将结果写回 state["notifications_sent"]。
    """
    fault_id = state.get("fault_id", "UNKNOWN")
    service_name = state.get("service_name", "unknown")
    fault_description = state.get("fault_description", "")
    analysis_result = state.get("analysis_result", "")
    root_cause = state.get("root_cause", "")
    recovery_actions = state.get("recovery_actions", "")
    is_resolved = state.get("is_resolved", False)
    error_message = state.get("error_message")

    logger.info(
        f"[NotificationAgent] 发送通知 fault_id={fault_id} is_resolved={is_resolved}"
    )

    if is_resolved:
        notification_type = "恢复通知"
        status_desc = "✅ 故障已恢复"
    elif error_message:
        notification_type = "告警通知（处理异常）"
        status_desc = f"⚠️ 处理过程中发生异常，需人工介入"
    else:
        notification_type = "告警通知（处理中）"
        status_desc = "🔧 故障正在处理中"

    prompt = f"""请发送以下故障{notification_type}：

## 故障信息
- 故障 ID：{fault_id}
- 服务名称：{service_name}
- 故障描述：{fault_description}
- 当前状态：{status_desc}

## 根因分析
{root_cause or "（分析中）"}

## 已执行操作
{recovery_actions or "（尚未执行恢复操作）"}

{f"## 异常信息" + chr(10) + error_message if error_message else ""}

请调用 send_notification 工具发送通知。"""

    try:
        agent = create_notification_agent(fault_id=fault_id)
        result = agent.invoke(
            {"messages": [HumanMessage(content=prompt)]},
            config=RunnableConfig(
                configurable={"thread_id": f"{fault_id}-notify"},
            ),
        )
        messages = result.get("messages", [])
        logger.info(f"[NotificationAgent] 通知发送完成 fault_id={fault_id}")
        return {
            "notifications_sent": True,
            "messages": messages[-1:] if messages else [],
        }
    except Exception as e:
        logger.error(f"[NotificationAgent] 通知发送失败 fault_id={fault_id}: {e}")
        return {
            "notifications_sent": False,
            "error_message": f"NotificationAgent 异常: {e}",
        }
