"""
协调器 - 用 LangGraph StateGraph 编排多 Agent 流程

流程图：
  START
    ↓
  monitor_node (MonitorAgent: 采集全链路状态)
    ↓
  analysis_node (AnalysisAgent: 根因分析 + 制定方案)
    ↓
  recovery_node (RecoveryAgent: 执行恢复操作)
    ↓
  notify_node (NotificationAgent: 发送通知)
    ↓
  should_verify? ──YES──→ monitor_node (回环验证，最多 MAX_VERIFY_COUNT 次)
    ↓NO
  END

每个节点职责单一，通过 FaultState 传递数据，
上下文不会因单节点消息累积而膨胀。
"""
import functools
import logging
from typing import Any, Dict, Literal

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph

from agents.state import FaultState
from agents.monitor_agent import run_monitor_node
from agents.analysis_agent import run_analysis_node
from agents.recovery_agent import run_recovery_node
from agents.notification_agent import run_notification_node

logger = logging.getLogger(__name__)

# 最大恢复验证次数，防止无限循环
MAX_VERIFY_COUNT = 2


def _make_recovery_node(console_confirm_mode: bool):
    """
    工厂方法：创建绑定了 console_confirm_mode 的恢复节点函数

    Args:
        console_confirm_mode: 危险操作是否使用控制台确认
    """
    def recovery_node(state: FaultState, config: RunnableConfig) -> Dict[str, Any]:
        return run_recovery_node(state, config, console_confirm_mode=console_confirm_mode)
    recovery_node.__name__ = "recovery_node"
    return recovery_node


def _route_after_recovery(state: FaultState) -> Literal["notify_node", "monitor_node", END]:
    """
    恢复节点后的路由决策：
    - 故障已恢复 → 发送恢复通知 → END
    - 故障未恢复 + 未超验证上限 → 重新监控（回环）
    - 故障未恢复 + 超验证上限 → 强制发送通知（告知人工介入）
    """
    is_resolved = state.get("is_resolved", False)
    verify_count = state.get("verify_count", 0)

    if is_resolved:
        logger.info(f"[Coordinator] 故障已恢复 → notify_node")
        return "notify_node"
    elif verify_count < MAX_VERIFY_COUNT:
        logger.info(
            f"[Coordinator] 故障未恢复（verify_count={verify_count}）→ 回环监控"
        )
        return "monitor_node"
    else:
        logger.warning(
            f"[Coordinator] 达到最大验证次数 ({MAX_VERIFY_COUNT})，强制通知"
        )
        return "notify_node"


def _increment_verify_count(state: FaultState) -> Dict[str, Any]:
    """辅助节点：每次回环时递增验证计数器"""
    return {"verify_count": state.get("verify_count", 0) + 1}


def build_fault_graph(console_confirm_mode: bool = True) -> Any:
    """
    构建故障处理多 Agent StateGraph

    Args:
        console_confirm_mode: RecoveryAgent 中危险操作是否使用控制台确认

    Returns:
        编译后的 CompiledStateGraph
    """
    builder = StateGraph(FaultState)

    # ===== 注册节点 =====
    builder.add_node("monitor_node", run_monitor_node)
    builder.add_node("analysis_node", run_analysis_node)
    builder.add_node("recovery_node", _make_recovery_node(console_confirm_mode))
    builder.add_node("notify_node", run_notification_node)
    builder.add_node("verify_counter", _increment_verify_count)

    # ===== 固定边 =====
    builder.add_edge(START, "monitor_node")
    builder.add_edge("monitor_node", "analysis_node")
    builder.add_edge("analysis_node", "recovery_node")

    # ===== 条件路由（恢复后决策）=====
    builder.add_conditional_edges(
        "recovery_node",
        _route_after_recovery,
        {
            "notify_node": "notify_node",
            "monitor_node": "verify_counter",  # 先递增计数器再回环
        },
    )
    builder.add_edge("verify_counter", "monitor_node")
    builder.add_edge("notify_node", END)

    # 使用 InMemorySaver 作为 checkpointer（LangGraph 1.x）
    graph = builder.compile(checkpointer=InMemorySaver())
    logger.info("[Coordinator] 故障处理 StateGraph 编译完成")
    return graph


# 全局 Graph 缓存（避免重复编译）
_graph_cache: Dict[bool, Any] = {}


def get_fault_graph(console_confirm_mode: bool = True) -> Any:
    """获取编译好的 Graph（带缓存）"""
    if console_confirm_mode not in _graph_cache:
        _graph_cache[console_confirm_mode] = build_fault_graph(console_confirm_mode)
    return _graph_cache[console_confirm_mode]
