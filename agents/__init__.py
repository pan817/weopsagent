"""
agents 包 - 主 Agent + 子 Agent 故障处理模块

架构：FaultAgent (主Agent) 通过 tools 调用 4 个子 Agent：
- monitor_agent: 全链路状态采集（run_monitoring tool）
- analysis_agent: 根因分析 + 方案制定（run_analysis tool）
- recovery_agent: 故障恢复操作执行（run_recovery tool）
- notification_agent: 通知发送（run_notification tool）
- fault_agent: 主 Agent 封装（对外入口）
"""
from agents.fault_agent import FaultAgent

__all__ = ["FaultAgent"]
