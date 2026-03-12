"""
故障处理主 Agent - 通过 tools 调用子 Agent 架构

FaultAgent 作为主 Agent，绑定 4 个子 Agent 工具（run_monitoring / run_analysis /
run_recovery / run_notification），由主 Agent 的 LLM 自主决定调用顺序，
system prompt 引导标准工作流程。

架构：
  FaultAgent (create_agent)
    ├── run_monitoring   → MonitorAgent (子Agent，内含 5 个监控工具)
    ├── run_analysis     → AnalysisAgent (子Agent，纯 LLM 推理)
    ├── run_recovery     → RecoveryAgent (子Agent，含危险操作 + 人工确认)
    └── run_notification → NotificationAgent (子Agent，通知发送)
"""
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from langchain.agents import create_agent
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver

from agents.monitor_agent import run_monitoring
from agents.analysis_agent import run_analysis
from agents.recovery_agent import run_recovery, set_console_confirm_mode
from agents.notification_agent import run_notification
from llm.model import get_llm
from memory.long_term import get_long_term_memory
from middleware.audit_log import AuditLogMiddleware
from middleware.model_switch import ModelSwitchMiddleware, ModelRule
from middleware.rate_limit import RateLimitMiddleware
from middleware.sliding_window import SlidingWindowMiddleware
from middleware.summarization import SummarizationMiddleware
from middleware.tool_input_fix import ToolInputFixMiddleware
from planner.fault_planner import FaultPlanner

logger = logging.getLogger(__name__)

# 主 Agent 的子 Agent 工具列表
FAULT_AGENT_TOOLS = [run_monitoring, run_analysis, run_recovery, run_notification]


def _load_prompt() -> str:
    """从 agents/prompts/fault_agent.txt 加载主 Agent System Prompt"""
    prompt_path = Path(__file__).parent / "prompts" / "fault_agent.txt"
    return prompt_path.read_text(encoding="utf-8").strip()


class FaultAgent:
    """
    故障处理主 Agent

    通过 create_agent 创建主 Agent，绑定 4 个子 Agent 工具，
    由主 Agent 的 LLM 按 system prompt 引导的流程自主调度子 Agent。

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
        model_rules: list = None,
    ):
        self.console_confirm_mode = console_confirm_mode
        self.enable_audit_log = enable_audit_log
        self.model_rules = model_rules

        # 设置 RecoveryAgent 的确认模式
        set_console_confirm_mode(console_confirm_mode)

        self.planner = FaultPlanner()
        self.long_term_memory = get_long_term_memory()

        # 构建主 Agent（checkpointer=InMemorySaver 自动管理对话历史）
        self._checkpointer = InMemorySaver()
        self._agent = self._build_agent()

        # 跟踪活跃会话 ID（供 API 会话管理接口使用）
        self._active_sessions: set = set()

        self._init_knowledge_base()
        logger.info("[FaultAgent] 主 Agent 初始化完成")

    def _build_agent(self) -> Any:
        """构建主 Agent（create_agent + 子 Agent 工具 + 中间件）"""
        from config.settings import settings

        middleware = [ToolInputFixMiddleware()]
        if self.enable_audit_log:
            middleware.append(AuditLogMiddleware())

        # 动态模型切换中间件（before_model 阶段根据规则替换模型）
        if self.model_rules:
            middleware.append(ModelSwitchMiddleware(rules=self.model_rules))

        # 限流中间件（before_model + wrap_tool_call 阶段限制调用频率）
        if settings.rate_limit_model_rpm or settings.rate_limit_tool_rpm:
            middleware.append(RateLimitMiddleware(
                model_rpm=settings.rate_limit_model_rpm,
                tool_rpm=settings.rate_limit_tool_rpm,
                strategy=settings.rate_limit_strategy,
                wait_timeout=settings.rate_limit_wait_timeout,
            ))

        # 短期记忆压缩（二选一：滑动窗口 vs LLM 摘要）
        if settings.sliding_window_enabled:
            middleware.append(SlidingWindowMiddleware(
                max_messages=settings.sliding_window_max_messages,
                preserve_recent=settings.sliding_window_preserve_recent,
                preserve_first=settings.sliding_window_preserve_first,
            ))
        elif settings.summarization_enabled:
            middleware.append(SummarizationMiddleware(
                max_messages=settings.summarization_max_messages,
                max_tokens=settings.summarization_max_tokens,
                preserve_recent=settings.summarization_preserve_recent,
            ))

        return create_agent(
            model=get_llm(),
            tools=FAULT_AGENT_TOOLS,
            system_prompt=_load_prompt(),
            middleware=middleware,
            checkpointer=self._checkpointer,
            name="fault_agent",
        )

    def _init_knowledge_base(self) -> None:
        """初始化长期记忆知识库"""
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

        流程：
        1. FaultPlanner 推断服务名、告警类型、拓扑信息
        2. RAG 检索相关知识
        3. 构建提示并调用主 Agent（主 Agent 自主调度子 Agent 工具）
        4. 提取处理结果（对话历史由 checkpointer 自动管理）
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

        # 3. 构建主 Agent 输入提示
        prompt = self._build_prompt(
            fault_description=fault_description,
            service_name=plan.service_name,
            service_node_info=plan.raw_service_info,
            knowledge_context=knowledge_context,
            fault_id=fault_id,
        )

        # 4. 调用主 Agent
        config = RunnableConfig(
            configurable={
                "thread_id": session_id,
                "fault_id": fault_id,
            },
            tags=[f"fault_id:{fault_id}", f"service:{plan.service_name}"],
        )

        try:
            result = self._agent.invoke(
                {"messages": [HumanMessage(content=prompt)]},
                config=config,
            )

            messages = result.get("messages", [])
            response = self._extract_last_text(messages)

            # 记录活跃会话（对话历史由 checkpointer 自动管理）
            self._active_sessions.add(session_id)

            elapsed = time.time() - start_time
            logger.info(
                f"[FaultAgent] 故障处理完成 fault_id={fault_id} elapsed={elapsed:.1f}s"
            )

            # 从响应文本判断处理状态
            is_resolved = self._check_status(response)
            status = "resolved" if is_resolved else "completed"

            return {
                "fault_id": fault_id,
                "session_id": session_id,
                "service_name": plan.service_name,
                "alert_type": plan.alert_type.value,
                "response": response,
                "elapsed_seconds": round(elapsed, 1),
                "status": status,
                "is_resolved": is_resolved,
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
        继续已有故障处理对话

        checkpointer 根据 thread_id (session_id) 自动恢复之前的对话上下文，
        无需手动加载历史消息。
        """
        return self.handle_fault(
            fault_description=user_input,
            session_id=session_id,
        )

    def list_sessions(self) -> list:
        """列出所有活跃会话 ID"""
        return sorted(self._active_sessions)

    def clear_session(self, session_id: str) -> None:
        """清除指定会话（从活跃列表中移除）"""
        self._active_sessions.discard(session_id)
        logger.info(f"[FaultAgent] 已清除会话 {session_id}")

    def _build_prompt(
        self,
        fault_description: str,
        service_name: str,
        service_node_info: str,
        knowledge_context: str,
        fault_id: str,
    ) -> str:
        """构建主 Agent 的输入提示"""
        return f"""## 故障事件
- 故障 ID：{fault_id}
- 服务名称：{service_name}
- 故障描述：{fault_description}

## 服务依赖信息
{service_node_info or "（暂无服务拓扑信息）"}

## 知识库参考（RAG 检索结果）
{knowledge_context or "（无相关知识）"}

请按照工作流程处理这个故障。"""

    def _retrieve_knowledge(self, query: str) -> str:
        """从长期记忆检索相关知识"""
        try:
            return self.long_term_memory.format_context(query, top_k=6)
        except Exception as e:
            logger.warning(f"[FaultAgent] 知识库检索失败: {e}")
            return "（知识库检索失败）"

    @staticmethod
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
        return "故障处理流程已完成，请查看详细日志。"

    @staticmethod
    def _check_status(response: str) -> bool:
        """从主 Agent 响应文本中判断故障是否已恢复"""
        text_lower = response.lower()
        if any(kw in text_lower for kw in ["partial", "failed", "未恢复", "恢复失败", "需人工"]):
            return False
        resolved_keywords = ["resolved", "已恢复", "恢复成功", "故障已消除"]
        for kw in resolved_keywords:
            if kw in text_lower:
                negative_prefix = any(
                    neg + kw in text_lower for neg in ["未", "没有", "尚未", "仍未"]
                )
                if not negative_prefix:
                    return True
        return False
