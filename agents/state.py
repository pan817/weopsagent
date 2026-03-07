"""
多 Agent 共享状态定义

FaultState 是贯穿整个多 Agent 流程的共享状态，
通过 LangGraph StateGraph 在各节点之间传递和更新。

各 Agent 只读取自身需要的字段，只写入自身负责的字段，
从而实现上下文隔离、避免单 Agent 上下文膨胀。
"""
from typing import Annotated, Any, Dict, List, Optional
from typing_extensions import TypedDict

from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages


class FaultState(TypedDict):
    """
    故障处理全流程共享状态

    流转路径：
    START → monitor_node → analysis_node → recovery_node → notify_node → END

    每个节点只更新自身负责的字段：
    - monitor_node:      monitoring_results, alert_type
    - analysis_node:     analysis_result, root_cause, recovery_plan
    - recovery_node:     recovery_actions, is_resolved
    - notify_node:       notifications_sent
    """

    # ===== 基础信息（入参，由 FaultAgent 初始化）=====
    fault_id: str                       # 故障唯一 ID
    fault_description: str              # 故障原始描述
    service_name: str                   # 推断出的服务名称
    session_id: str                     # 短期记忆隔离 key
    knowledge_context: str              # 知识库检索结果（RAG）
    service_node_info: str              # 服务拓扑信息

    # ===== MonitorAgent 输出 =====
    alert_type: str                     # 告警类型（如 api_slow）
    monitoring_results: str             # 全链路监控摘要（JSON 文本）

    # ===== AnalysisAgent 输出 =====
    analysis_result: str                # 故障原因分析结论
    root_cause: str                     # 根因定位
    recovery_plan: str                  # 建议的恢复方案

    # ===== RecoveryAgent 输出 =====
    recovery_actions: str               # 已执行的恢复操作摘要
    is_resolved: bool                   # 故障是否已恢复

    # ===== NotificationAgent 输出 =====
    notifications_sent: bool            # 是否已发送通知

    # ===== 流程控制 =====
    error_message: Optional[str]        # 任一节点的错误信息
    verify_count: int                   # 恢复验证次数（防无限循环）

    # ===== 消息历史（各 Agent 内部使用，add_messages 自动追加）=====
    messages: Annotated[List[BaseMessage], add_messages]
