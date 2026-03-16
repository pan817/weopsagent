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
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from langchain.agents import create_agent
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver

from agents.monitor_agent import run_monitoring
from agents.monitor_agent import purge_thread as monitor_purge_thread
from agents.analysis_agent import run_analysis
from agents.analysis_agent import purge_thread as analysis_purge_thread
from agents.recovery_agent import run_recovery, set_console_confirm_mode
from agents.recovery_agent import purge_thread as recovery_purge_thread
from agents.notification_agent import run_notification
from agents.notification_agent import purge_thread as notification_purge_thread
from core.context import get_correlation_id, new_correlation_id
from llm.model import get_llm
from memory.memory_manager import get_memory_manager
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
        self.memory_manager = get_memory_manager()

        # 构建主 Agent（checkpointer=InMemorySaver 自动管理对话历史）
        self._checkpointer = InMemorySaver()
        self._agent = self._build_agent()

        # 跟踪活跃会话 {session_id: last_active_timestamp}（线程安全）
        self._active_sessions: Dict[str, float] = {}
        self._sessions_lock = threading.Lock()
        # 会话过期时间（秒），默认 2 小时
        self._session_ttl: float = 7200.0
        # 最大会话数量，超过时清理最旧的会话
        self._max_sessions: int = 200

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
            counts = self.memory_manager._chroma.load_knowledge_base()
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

        # CLI 模式下 API 层未设置 correlation_id，自动生成保证全链路可追踪
        if not get_correlation_id():
            new_correlation_id()

        logger.info(f"[FaultAgent] 开始处理故障 fault_id={fault_id}")

        # 1. 故障规划
        plan = self.planner.create_plan(fault_id, fault_description)

        # 2. 三级知识检索（L1 Redis → L2 ES → L3 ChromaDB）
        knowledge_context = self._retrieve_knowledge(
            plan.knowledge_query,
            service_name=plan.service_name,
            alert_type=plan.alert_type.value,
        )

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

            # 记录活跃会话并清理过期会话
            with self._sessions_lock:
                self._active_sessions[session_id] = time.time()
                self._cleanup_expired_sessions()

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
        with self._sessions_lock:
            self._cleanup_expired_sessions()
            return sorted(self._active_sessions.keys())

    def clear_session(self, session_id: str) -> None:
        """清除指定会话（从活跃列表和 checkpointer 中移除）"""
        with self._sessions_lock:
            self._active_sessions.pop(session_id, None)
        # 尝试清理 checkpointer 中的会话数据
        self._purge_checkpointer_thread(session_id)
        logger.info(f"[FaultAgent] 已清除会话 {session_id}")

    def _purge_checkpointer_thread(self, thread_id: str) -> None:
        """从主 Agent 和所有子 Agent 的 InMemorySaver 中删除指定 thread 的检查点数据"""
        # 清理主 Agent checkpointer
        try:
            storage = getattr(self._checkpointer, "storage", None)
            if storage is not None and isinstance(storage, dict):
                keys_to_remove = [
                    k for k in storage if k[0] == thread_id
                ]
                for k in keys_to_remove:
                    del storage[k]
        except Exception as e:
            logger.debug(f"[FaultAgent] 清理主 Agent checkpointer 失败: {e}")

        # 清理所有子 Agent 的 checkpointer（子 Agent 以 fault_id 作为 thread_id）
        for purge_fn in (
            monitor_purge_thread,
            analysis_purge_thread,
            recovery_purge_thread,
            notification_purge_thread,
        ):
            try:
                purge_fn(thread_id)
            except Exception as e:
                logger.debug(f"[FaultAgent] 清理子 Agent checkpointer 失败: {e}")

    def _cleanup_expired_sessions(self) -> None:
        """清理过期会话（需在 _sessions_lock 内调用）"""
        now = time.time()
        expired = [
            sid for sid, ts in self._active_sessions.items()
            if now - ts > self._session_ttl
        ]
        for sid in expired:
            del self._active_sessions[sid]
            # 异步清理 checkpointer 避免在锁内做 IO
            threading.Thread(
                target=self._purge_checkpointer_thread,
                args=(sid,),
                daemon=True,
            ).start()

        # 如果超过最大会话数，移除最旧的
        if len(self._active_sessions) > self._max_sessions:
            sorted_sessions = sorted(
                self._active_sessions.items(), key=lambda x: x[1]
            )
            excess = len(self._active_sessions) - self._max_sessions
            for sid, _ in sorted_sessions[:excess]:
                del self._active_sessions[sid]
                threading.Thread(
                    target=self._purge_checkpointer_thread,
                    args=(sid,),
                    daemon=True,
                ).start()

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

    def _retrieve_knowledge(
        self,
        query: str,
        service_name: str = "",
        alert_type: str = "",
    ) -> str:
        """三级级联知识检索（L1 Redis → L2 ES → L3 ChromaDB）"""
        try:
            return self.memory_manager.search(
                query,
                service=service_name or None,
                alert_type=alert_type or None,
            )
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
