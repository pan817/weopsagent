"""
agents 包 - 多 Agent 协作故障处理模块

包含以下子模块：
- state: 共享 FaultState TypedDict
- monitor_agent: 全链路状态采集
- analysis_agent: 根因分析 + 方案制定
- recovery_agent: 故障恢复操作执行
- notification_agent: 通知发送
- coordinator: LangGraph StateGraph 协调器
- fault_agent: 高级 FaultAgent 封装（对外入口）
"""
from agents.fault_agent import FaultAgent
from agents.coordinator import build_fault_graph, get_fault_graph
from agents.state import FaultState

__all__ = ["FaultAgent", "build_fault_graph", "get_fault_graph", "FaultState"]
