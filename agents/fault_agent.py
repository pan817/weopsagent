"""
多 Agent 故障处理入口 - FaultAgent 高级封装

封装了基于 LangGraph StateGraph 的多 Agent 协作流程：
  1. FaultPlanner 推断服务名、告警类型、服务拓扑
  2. 从 LongTermMemory 检索相关知识（RAG）
  3. 构建初始 FaultState 并调用 StateGraph（monitor→analysis→recovery→notify）
  4. 从最终 State 中提取处理结果并返回

对外接口与 agent/fault_agent.py 保持兼容，可直接替换。
"""
import logging
import time
from datetime import datetime
from typing import Any, Dict, Optional

from langchain_core.runnables import RunnableConfig

from agents.coordinator import get_fault_graph
from memory.long_term import get_long_term_memory
from memory.short_term import get_short_term_memory
from planner.fault_planner import FaultPlanner

logger = logging.getLogger(__name__)


class FaultAgent:
    """
    多 Agent 故障处理的高级封装

    提供与单 Agent 版本相同的公共接口（handle_fault / continue_conversation），
    内部使用 LangGraph StateGraph 协调 MonitorAgent / AnalysisAgent /
    RecoveryAgent / NotificationAgent 四个专项子 Agent。

    Usage:
        agent = FaultAgent()
        result = agent.handle_fault(
            fault_description="订单服务响应超时，接口报500错误",
            session_id="fault-20240101-001"
        )
    """

    def __init__(
        self,
        console_confirm_mode: bool = True,
        enable_audit_log: bool = True,
    ):
        """
        初始化 FaultAgent

        Args:
            console_confirm_mode: RecoveryAgent 危险操作是否使用控制台交互确认
            enable_audit_log: 是否启用审计日志（传递给各子 Agent）
        """
        self.console_confirm_mode = console_confirm_mode
        self.enable_audit_log = enable_audit_log

        self.planner = FaultPlanner()
        self.short_term_memory = get_short_term_memory()
        self.long_term_memory = get_long_term_memory()

        self._init_knowledge_base()
        logger.info("[FaultAgent] 多 Agent FaultAgent 初始化完成")

    def _init_knowledge_base(self) -> None:
        """初始化长期记忆知识库（加载 data/ 目录下的 Markdown 文件）"""
        try:
            counts = self.long_term_memory.load_knowledge_base()
            logger.info(f"[FaultAgent] 知识库加载完成: {counts}")
        except Exception as e:
            logger.warning(f"[FaultAgent] 知识库加载失败（将继续运行）: {e}")

    def handle_fault(
        self,
        fault_description: str,
        session_id: Optional[str] = None,
        fault_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        处理一个故障事件

        完整流程：
        1. FaultPlanner 推断服务名、告警类型、拓扑信息
        2. RAG 检索相关知识
        3. 构建初始 FaultState
        4. 调用 StateGraph：monitor → analysis → recovery → notify（含验证回环）
        5. 从最终 State 提取结果并写入短期记忆

        Args:
            fault_description: 故障描述文本
            session_id: 会话 ID（用于短期记忆隔离）
            fault_id: 故障 ID（不提供则自动生成）

        Returns:
            处理结果字典（与单 Agent 版本兼容）
        """
        ts = datetime.now().strftime("%Y%m%d%H%M%S")
        fault_id = fault_id or f"FAULT-{ts}"
        session_id = session_id or fault_id
        start_time = time.time()

        logger.info(f"[FaultAgent] 开始处理故障 fault_id={fault_id}")

        # 1. 故障规划
        plan = self.planner.create_plan(fault_id, fault_description)

        # 2. RAG 知识检索
        knowledge_context = self._retrieve_knowledge(plan.knowledge_query)

        # 3. 构建初始 FaultState
        initial_state = {
            "fault_id": fault_id,
            "fault_description": fault_description,
            "service_name": plan.service_name,
            "session_id": session_id,
            "knowledge_context": knowledge_context,
            "service_node_info": plan.raw_service_info,
            "alert_type": plan.alert_type.value,
            "monitoring_results": "",
            "analysis_result": "",
            "root_cause": "",
            "recovery_plan": "",
            "recovery_actions": "",
            "is_resolved": False,
            "notifications_sent": False,
            "error_message": None,
            "verify_count": 0,
            "messages": [],
        }

        # 4. 获取 StateGraph 并执行
        graph = get_fault_graph(console_confirm_mode=self.console_confirm_mode)
        config = RunnableConfig(
            configurable={"thread_id": session_id},
            tags=[f"fault_id:{fault_id}", f"service:{plan.service_name}"],
        )

        try:
            final_state = graph.invoke(initial_state, config=config)

            # 5. 从最终 State 提取摘要响应
            response = self._extract_response(final_state)

            # 6. 将处理结果写入短期记忆
            self.short_term_memory.add_user_message(session_id, fault_description)
            self.short_term_memory.add_ai_message(session_id, response)

            elapsed = time.time() - start_time
            logger.info(
                f"[FaultAgent] 故障处理完成 fault_id={fault_id} "
                f"is_resolved={final_state.get('is_resolved', False)} elapsed={elapsed:.1f}s"
            )

            status = "resolved" if final_state.get("is_resolved", False) else "completed"
            error_message = final_state.get("error_message")
            if error_message:
                status = "error"

            return {
                "fault_id": fault_id,
                "session_id": session_id,
                "service_name": plan.service_name,
                "alert_type": plan.alert_type.value,
                "response": response,
                "elapsed_seconds": round(elapsed, 1),
                "status": status,
                "is_resolved": final_state.get("is_resolved", False),
                "notifications_sent": final_state.get("notifications_sent", False),
                "messages": [{"role": "assistant", "content": response}],
            }

        except Exception as e:
            elapsed = time.time() - start_time
            logger.error(
                f"[FaultAgent] 故障处理异常 fault_id={fault_id}: {e}", exc_info=True
            )
            return {
                "fault_id": fault_id,
                "session_id": session_id,
                "service_name": plan.service_name,
                "alert_type": plan.alert_type.value,
                "response": f"故障处理过程中发生异常: {str(e)}",
                "elapsed_seconds": round(elapsed, 1),
                "status": "error",
                "error": str(e),
            }

    def continue_conversation(
        self,
        session_id: str,
        user_input: str,
    ) -> Dict[str, Any]:
        """
        继续已有故障处理对话（多轮交互）

        Args:
            session_id: 已有的会话 ID
            user_input: 用户的追加输入

        Returns:
            处理结果字典
        """
        return self.handle_fault(
            fault_description=user_input,
            session_id=session_id,
        )

    def _retrieve_knowledge(self, query: str) -> str:
        """从长期记忆检索相关知识"""
        try:
            return self.long_term_memory.format_context(query, top_k=6)
        except Exception as e:
            logger.warning(f"[FaultAgent] 知识库检索失败: {e}")
            return "（知识库检索失败）"

    def _extract_response(self, state: Dict[str, Any]) -> str:
        """从最终 FaultState 中提取可读的处理摘要"""
        parts = []

        root_cause = state.get("root_cause", "").strip()
        if root_cause:
            parts.append(f"## 根因分析\n{root_cause}")

        recovery_actions = state.get("recovery_actions", "").strip()
        if recovery_actions:
            parts.append(f"## 已执行操作\n{recovery_actions}")

        is_resolved = state.get("is_resolved", False)
        notifications_sent = state.get("notifications_sent", False)
        error_message = state.get("error_message", "")

        if is_resolved:
            status_line = "✅ 故障已恢复"
        elif error_message:
            status_line = f"⚠️ 处理过程异常：{error_message}"
        else:
            status_line = "🔧 故障处理已完成，请持续关注服务状态"

        parts.append(f"## 处理状态\n{status_line}")

        if notifications_sent:
            parts.append("📢 通知已发送至相关人员")

        if not parts:
            analysis = state.get("analysis_result", "").strip()
            if analysis:
                return analysis
            return "故障处理流程已完成，请查看详细日志。"

        return "\n\n".join(parts)
