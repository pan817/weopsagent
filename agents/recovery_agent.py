"""
恢复 Agent - 专注故障处理与自愈操作

职责：
- 按照分析 Agent 制定的方案执行恢复操作
- 包含危险操作（service_restart），必须经人工确认
- 将有效的处理经验存入长期记忆

使用工具：service_restart（危险）、store_knowledge
"""
import logging
from typing import Any, Dict

from langchain.agents import create_agent
from langchain_core.messages import HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver

from llm.model import get_llm
from middleware.audit_log import AuditLogMiddleware
from middleware.human_confirm import HumanConfirmMiddleware
from tools.service_restart import ServiceRestartTool
from tools.knowledge_store import StoreKnowledgeTool

logger = logging.getLogger(__name__)

# 恢复 Agent 工具集（含危险工具，需人工确认）
RECOVERY_TOOLS = [
    ServiceRestartTool(),
    StoreKnowledgeTool(),
]

_SYSTEM_PROMPT = """你是专业的 WeOps 恢复 Agent，负责执行故障恢复操作。

## 你的职责
1. **执行恢复**：按照恢复方案逐步执行操作
2. **危险操作确认**：重启服务等危险操作会自动触发人工确认，等待授权后再执行
3. **存储经验**：故障成功恢复后，将有效经验存入知识库

## 执行原则
- 按优先级从高到低执行恢复方案
- 每步操作后评估是否达到预期效果
- 若当前步骤无效，继续下一步
- 所有操作完成后，给出明确的恢复状态结论

## 输出格式
请按以下结构输出：

### 执行操作
[逐一列出已执行的操作和结果]

### 恢复状态
[RESOLVED：故障已恢复 | PARTIAL：部分恢复 | FAILED：未能恢复]

### 后续建议
[如未完全恢复，给出下一步建议]"""


def create_recovery_agent(
    fault_id: str = None,
    console_confirm_mode: bool = True,
):
    """
    创建恢复 Agent

    Args:
        fault_id: 故障 ID（用于审计日志）
        console_confirm_mode: 危险操作是否使用控制台确认
    """
    return create_agent(
        model=get_llm(),
        tools=RECOVERY_TOOLS,
        system_prompt=_SYSTEM_PROMPT,
        middleware=[
            AuditLogMiddleware(fault_id=fault_id),
            HumanConfirmMiddleware(console_mode=console_confirm_mode),
        ],
        checkpointer=InMemorySaver(),
        name="recovery_agent",
    )


def run_recovery_node(
    state: Dict[str, Any],
    config: RunnableConfig,
    console_confirm_mode: bool = True,
) -> Dict[str, Any]:
    """
    恢复节点执行函数（LangGraph 节点）

    读取 analysis_result 和 recovery_plan，
    调用恢复 Agent 执行恢复操作，
    将结果写回 state["recovery_actions"] 和 state["is_resolved"]。
    """
    fault_id = state.get("fault_id", "UNKNOWN")
    fault_description = state.get("fault_description", "")
    analysis_result = state.get("analysis_result", "")
    recovery_plan = state.get("recovery_plan", "")
    root_cause = state.get("root_cause", "")
    service_name = state.get("service_name", "unknown")

    logger.info(f"[RecoveryAgent] 开始执行恢复 fault_id={fault_id}")

    prompt = f"""## 故障信息
- 服务名称：{service_name}
- 故障描述：{fault_description}

## 根因分析
{root_cause}

## 恢复方案
{recovery_plan}

## 完整分析结果（参考）
{analysis_result}

请按照恢复方案执行操作。注意：
1. 重启服务等危险操作需要人工确认，系统会自动暂停等待
2. 操作完成后请调用 store_knowledge 工具保存本次处理经验
3. 最后给出明确的恢复状态结论（RESOLVED/PARTIAL/FAILED）"""

    try:
        agent = create_recovery_agent(
            fault_id=fault_id,
            console_confirm_mode=console_confirm_mode,
        )
        result = agent.invoke(
            {"messages": [HumanMessage(content=prompt)]},
            config=RunnableConfig(
                configurable={"thread_id": f"{fault_id}-recovery"},
            ),
        )
        messages = result.get("messages", [])
        recovery_text = _extract_last_text(messages)

        # 判断是否已恢复
        is_resolved = _check_resolved(recovery_text)
        logger.info(
            f"[RecoveryAgent] 恢复完成 fault_id={fault_id} is_resolved={is_resolved}"
        )

        return {
            "recovery_actions": recovery_text,
            "is_resolved": is_resolved,
            "messages": messages[-1:] if messages else [],
        }

    except PermissionError as e:
        # 危险操作被人工拒绝
        logger.warning(f"[RecoveryAgent] 危险操作被拒绝 fault_id={fault_id}: {e}")
        return {
            "recovery_actions": f"操作被拒绝：{e}",
            "is_resolved": False,
            "error_message": f"操作被拒绝: {e}",
        }
    except Exception as e:
        logger.error(f"[RecoveryAgent] 执行恢复失败 fault_id={fault_id}: {e}")
        return {
            "recovery_actions": f"恢复执行失败: {e}",
            "is_resolved": False,
            "error_message": f"RecoveryAgent 异常: {e}",
        }


def _extract_last_text(messages) -> str:
    from langchain_core.messages import AIMessage
    for msg in reversed(messages):
        if isinstance(msg, AIMessage) and msg.content:
            if isinstance(msg.content, str):
                return msg.content
            elif isinstance(msg.content, list):
                texts = [p.get("text", "") for p in msg.content if isinstance(p, dict) and p.get("type") == "text"]
                return "\n".join(texts)
    return "（恢复 Agent 未返回有效结果）"


def _check_resolved(recovery_text: str) -> bool:
    """从恢复 Agent 输出文本中判断故障是否已恢复"""
    text_lower = recovery_text.lower()
    resolved_keywords = ["resolved", "已恢复", "恢复成功", "故障已消除", "服务正常"]
    failed_keywords = ["failed", "未能恢复", "恢复失败", "仍然异常", "partial"]

    for kw in resolved_keywords:
        if kw in text_lower:
            # 检查是否被否定词否定
            if "未" + kw not in text_lower and "没有" + kw not in text_lower:
                return True
    return False
