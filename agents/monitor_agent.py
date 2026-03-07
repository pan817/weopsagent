"""
监控 Agent - 专注全链路状态数据采集

职责：
- 调用 5 个监控工具收集基础设施数据
- 汇总进程、Redis、MQ、数据库、日志的监控结果
- 只生产监控数据，不做故障分析

使用工具：
  process_monitor, redis_monitor, mq_monitor, db_monitor, log_analyzer
"""
import logging
from typing import Any, Dict

from langchain.agents import create_agent
from langchain_core.messages import HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver

from llm.model import get_llm
from middleware.audit_log import AuditLogMiddleware
from tools.process_monitor import ProcessMonitorTool
from tools.redis_monitor import RedisMonitorTool
from tools.mq_monitor import MQMonitorTool
from tools.db_monitor import DBMonitorTool
from tools.log_analyzer import LogAnalyzerTool

logger = logging.getLogger(__name__)

# 监控 Agent 专用工具集（只含采集类工具，无危险操作）
MONITOR_TOOLS = [
    ProcessMonitorTool(),
    RedisMonitorTool(),
    MQMonitorTool(),
    DBMonitorTool(),
    LogAnalyzerTool(),
]

_SYSTEM_PROMPT = """你是专业的 WeOps 监控 Agent，负责采集服务全链路的运行状态数据。

## 你的职责
对以下中间件逐一进行状态监控，收集原始数据（不做故障分析）：
1. **应用进程**：检查进程是否存在、CPU/内存使用率
2. **Redis**：连接数、内存使用、命中率、慢操作
3. **消息队列（MQ）**：队列积压量、消费者数量、死信情况
4. **数据库**：连接池状态、慢查询、锁等待
5. **应用日志**：最近错误日志、异常统计

## 输出要求
- 逐一调用工具，不要跳过任何中间件
- 以结构化方式输出各工具的监控结果
- 标注哪些指标异常（如连接数满、错误率高、进程不存在）
- 最后输出一份简洁的全链路监控摘要"""


def create_monitor_agent(fault_id: str = None):
    """创建监控 Agent"""
    return create_agent(
        model=get_llm(),
        tools=MONITOR_TOOLS,
        system_prompt=_SYSTEM_PROMPT,
        middleware=[AuditLogMiddleware(fault_id=fault_id)],
        checkpointer=InMemorySaver(),
        name="monitor_agent",
    )


def run_monitor_node(state: Dict[str, Any], config: RunnableConfig) -> Dict[str, Any]:
    """
    监控节点执行函数（LangGraph 节点）

    读取 state 中的服务信息，调用监控 Agent 采集数据，
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
        agent = create_monitor_agent(fault_id=fault_id)
        result = agent.invoke(
            {"messages": [HumanMessage(content=prompt)]},
            config=RunnableConfig(
                configurable={"thread_id": f"{fault_id}-monitor"},
            ),
        )
        # 提取监控摘要
        messages = result.get("messages", [])
        monitoring_results = _extract_last_text(messages)
        logger.info(f"[MonitorAgent] 监控完成 fault_id={fault_id}")

        return {
            "monitoring_results": monitoring_results,
            "messages": messages[-2:] if len(messages) >= 2 else messages,  # 只追加最后的 AI 摘要
        }
    except Exception as e:
        logger.error(f"[MonitorAgent] 监控失败 fault_id={fault_id}: {e}")
        return {
            "monitoring_results": f"监控采集失败: {e}",
            "error_message": f"MonitorAgent 异常: {e}",
        }


def _extract_last_text(messages) -> str:
    """从消息列表提取最后一条 AI 文本响应"""
    from langchain_core.messages import AIMessage
    for msg in reversed(messages):
        if isinstance(msg, AIMessage) and msg.content:
            if isinstance(msg.content, str):
                return msg.content
            elif isinstance(msg.content, list):
                texts = [p.get("text", "") for p in msg.content if isinstance(p, dict) and p.get("type") == "text"]
                return "\n".join(texts)
    return "（监控 Agent 未返回有效结果）"
