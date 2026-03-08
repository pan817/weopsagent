"""
监控 Subagent - 全链路状态数据采集

Subagent 设计要点：
- 编译后的 Agent 实例在进程生命周期内缓存复用，不在每次调用时重建
- Prompt 从 agents/prompts/monitor_agent.txt 加载，便于维护
- AuditLogMiddleware 不绑定静态 fault_id，由 RunnableConfig.configurable 动态注入
- 工具集仅含只读监控工具，无任何危险操作

使用工具：monitor_process / analyze_logs / monitor_redis / monitor_mq / monitor_database
"""
import logging
from pathlib import Path
from typing import Any, Dict

from langchain.agents import create_agent
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver

from llm.model import get_llm
from middleware.audit_log import AuditLogMiddleware
from tools import get_tool_registry

logger = logging.getLogger(__name__)

# 监控 Subagent 专用工具集（只含采集类工具，无危险操作）
MONITOR_TOOLS = get_tool_registry().get_group("monitor")

# ===== 单例缓存 =====
_agent: Any = None


def _load_prompt() -> str:
    """从 agents/prompts/monitor_agent.txt 加载 System Prompt"""
    prompt_path = Path(__file__).parent / "prompts" / "monitor_agent.txt"
    return prompt_path.read_text(encoding="utf-8").strip()


def _get_agent() -> Any:
    """
    获取 Monitor Subagent 编译实例（进程级单例）

    Agent 只在首次调用时编译，后续调用复用同一实例。
    fault_id 通过 RunnableConfig.configurable["fault_id"] 在调用时动态注入，
    无需重建 Agent 即可支持多个并发故障处理。
    """
    global _agent
    if _agent is None:
        logger.info("[MonitorAgent] 编译 Monitor Subagent（首次初始化）")
        _agent = create_agent(
            model=get_llm(),
            tools=MONITOR_TOOLS,
            system_prompt=_load_prompt(),
            # AuditLogMiddleware 不绑定 fault_id，由运行时从 configurable 读取
            middleware=[AuditLogMiddleware()],
            checkpointer=InMemorySaver(),
            name="monitor_agent",
        )
    return _agent


def run_monitor_node(state: Dict[str, Any], config: RunnableConfig) -> Dict[str, Any]:
    """
    监控节点（LangGraph 节点函数）

    从 FaultState 读取服务信息，调用 Monitor Subagent 采集全链路状态，
    将结果写回 state["monitoring_results"]。
    """
    fault_id = state.get("fault_id", "UNKNOWN")
    service_name = state.get("service_name", "unknown")
    service_node_info = state.get("service_node_info", "")
    fault_description = state.get("fault_description", "")

    logger.info(f"[MonitorAgent] 开始监控 service={service_name} fault_id={fault_id}")

    prompt = (
        f"故障描述：{fault_description}\n\n"
        f"服务依赖信息：\n{service_node_info}\n\n"
        f"请对该服务的全链路进行监控，采集所有中间件的运行状态。"
    )

    try:
        agent = _get_agent()
        result = agent.invoke(
            {"messages": [HumanMessage(content=prompt)]},
            config=RunnableConfig(
                configurable={
                    "thread_id": f"{fault_id}-monitor",
                    "fault_id": fault_id,   # 供 AuditLogMiddleware 动态读取
                },
            ),
        )
        messages = result.get("messages", [])
        monitoring_results = _extract_last_text(messages)
        logger.info(f"[MonitorAgent] 监控完成 fault_id={fault_id}")

        return {
            "monitoring_results": monitoring_results,
            "messages": messages[-2:] if len(messages) >= 2 else messages,
        }
    except Exception as e:
        logger.error(f"[MonitorAgent] 监控失败 fault_id={fault_id}: {e}")
        return {
            "monitoring_results": f"监控采集失败: {e}",
            "error_message": f"MonitorAgent 异常: {e}",
        }


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
