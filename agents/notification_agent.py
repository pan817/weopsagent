"""
通知 Subagent - 故障告警与恢复通知发送

Subagent 设计要点：
- 编译后的 Agent 实例在进程生命周期内缓存复用，不在每次调用时重建
- Prompt 从 agents/prompts/notification_agent.txt 加载，便于维护
- AuditLogMiddleware 不绑定静态 fault_id，由 RunnableConfig.configurable 动态注入
- 根据 is_resolved 状态自动选择通知类型（告警/恢复/升级）

使用工具：send_notification（支持 DingTalk / Slack / Email）
"""
import logging
from pathlib import Path
from typing import Any, Dict

from langchain.agents import create_agent
from langchain_core.messages import HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver

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
    """
    获取 Notification Subagent 编译实例（进程级单例）

    fault_id 通过 RunnableConfig.configurable["fault_id"] 在调用时动态注入。
    """
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


def run_notification_node(state: Dict[str, Any], config: RunnableConfig) -> Dict[str, Any]:
    """
    通知节点（LangGraph 节点函数）

    根据 FaultState 中的 is_resolved 状态自动判断通知类型：
    - is_resolved=True  → 恢复通知
    - error_message 存在 → 升级通知（需人工介入）
    - 其他             → 故障告警通知
    """
    fault_id = state.get("fault_id", "UNKNOWN")
    service_name = state.get("service_name", "unknown")
    fault_description = state.get("fault_description", "")
    root_cause = state.get("root_cause", "")
    recovery_actions = state.get("recovery_actions", "")
    is_resolved = state.get("is_resolved", False)
    error_message = state.get("error_message")

    logger.info(
        f"[NotificationAgent] 发送通知 fault_id={fault_id} is_resolved={is_resolved}"
    )

    # 根据状态确定通知类型和状态描述
    if is_resolved:
        notification_type = "恢复通知"
        status_desc = "✅ 故障已恢复"
    elif error_message:
        notification_type = "升级通知（需人工介入）"
        status_desc = "⚠️ 自动处理失败，需要人工介入"
    else:
        notification_type = "故障告警通知"
        status_desc = "🔴 故障正在处理中"

    prompt = f"""请发送以下{notification_type}：

## 故障信息
- 故障 ID：{fault_id}
- 服务名称：{service_name}
- 故障描述：{fault_description}
- 当前状态：{status_desc}

## 根因分析
{root_cause or "（分析中）"}

## 已执行操作
{recovery_actions or "（尚未执行恢复操作）"}

{("## 异常信息\n" + error_message) if error_message else ""}

请根据通知类型选择合适的模板，调用 send_notification 工具发送通知。"""

    try:
        agent = _get_agent()
        result = agent.invoke(
            {"messages": [HumanMessage(content=prompt)]},
            config=RunnableConfig(
                configurable={
                    "thread_id": f"{fault_id}-notify",
                    "fault_id": fault_id,   # 供 AuditLogMiddleware 动态读取
                },
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
