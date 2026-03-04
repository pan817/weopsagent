"""
故障处理 Agent 模块 - 基于 LangChain 1.2.x create_agent 构建的智能故障处理 Agent

使用 LangChain 1.2.x 的 create_agent 函数创建 Agent，
相较于旧版 create_react_agent，新版通过原生 middleware 参数直接注入：
- tools: 所有监控和处理工具
- middleware: [AuditLogMiddleware, HumanConfirmMiddleware]（无需 callbacks 传递）
- checkpointer: InMemorySaver（状态持久化）
- system_prompt: 含服务依赖信息和知识库上下文的系统 Prompt
"""
import logging
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

from langchain.agents import create_agent
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver

from config.settings import settings
from llm.model import get_llm
from memory.long_term import get_long_term_memory
from memory.short_term import get_short_term_memory
from messages.handler import build_fault_input_message
from middleware.audit_log import AuditLogMiddleware
from middleware.human_confirm import HumanConfirmMiddleware
from planner.fault_planner import FaultPlanner
from tools import get_all_tools

logger = logging.getLogger(__name__)


def create_fault_agent(
    system_prompt: str,
    fault_id: Optional[str] = None,
    console_confirm_mode: bool = True,
    enable_audit_log: bool = True,
):
    """
    工厂函数：创建故障处理 Agent（LangChain 1.2.x）

    使用 LangChain 1.2.x create_agent 创建 Agent，直接注入：
    - model: ChatOpenAI
    - tools: 所有监控和处理工具
    - system_prompt: 含服务信息和知识库上下文的系统提示词
    - middleware: [AuditLogMiddleware, HumanConfirmMiddleware]
    - checkpointer: InMemorySaver（LangGraph 1.x 重命名自 MemorySaver）

    Args:
        system_prompt: 动态构建的系统 Prompt（含服务拓扑和知识库内容）
        fault_id: 故障 ID（注入审计日志中间件）
        console_confirm_mode: 是否使用控制台交互进行人工确认
        enable_audit_log: 是否启用审计日志中间件

    Returns:
        CompiledStateGraph: LangChain 1.2.x Agent 可执行对象
    """
    llm = get_llm()
    tools = get_all_tools()

    # 构建 middleware 列表（LangChain 1.2.x 原生参数）
    middleware = []
    if enable_audit_log:
        middleware.append(AuditLogMiddleware(fault_id=fault_id))
    middleware.append(
        HumanConfirmMiddleware(console_mode=console_confirm_mode)
    )

    # InMemorySaver：LangGraph 1.x 中 MemorySaver 的新名称
    checkpointer = InMemorySaver()

    # LangChain 1.2.x create_agent：middleware 作为一等公民入参
    agent = create_agent(
        model=llm,
        tools=tools,
        system_prompt=system_prompt,
        middleware=middleware,
        checkpointer=checkpointer,
    )

    logger.info(
        f"[FaultAgent] Agent 创建完成 fault_id={fault_id} "
        f"tools={[t.name for t in tools]} "
        f"middleware={[type(m).__name__ for m in middleware]}"
    )
    return agent


class FaultAgent:
    """
    故障处理 Agent 的高级封装

    提供以下功能：
    1. 故障规划（推断服务名、告警类型、全链路拓扑）
    2. 动态系统 Prompt（含服务依赖信息和 RAG 知识库上下文）
    3. 短期记忆（跨轮对话历史，按 session_id 隔离）
    4. 长期记忆（ChromaDB RAG 知识库检索）
    5. Middleware（审计日志 + 人工确认，通过 create_agent 原生注入）
    6. 故障处理后验证恢复状态

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
            console_confirm_mode: 危险操作是否使用控制台交互确认
            enable_audit_log: 是否启用审计日志中间件
        """
        self.console_confirm_mode = console_confirm_mode
        self.enable_audit_log = enable_audit_log

        # 核心组件
        self.planner = FaultPlanner()
        self.short_term_memory = get_short_term_memory()
        self.long_term_memory = get_long_term_memory()

        # 初始化知识库
        self._init_knowledge_base()

        logger.info("[FaultAgent] FaultAgent 初始化完成")

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
        处理一个故障事件（同步接口）

        完整处理流程：
        1. 创建故障分析计划（推断服务名、告警类型）
        2. 检索长期记忆知识库（RAG）
        3. 构建动态系统 Prompt（含服务拓扑 + 知识库内容）
        4. 通过 create_agent 注入 middleware 创建 Agent
        5. 调用 Agent 执行（工具调用分析和处理）
        6. 将对话存入短期记忆
        7. 返回处理结果

        Args:
            fault_description: 故障描述文本
            session_id: 会话 ID（用于短期记忆隔离）
            fault_id: 故障 ID（不提供则自动生成）

        Returns:
            处理结果字典
        """
        ts = datetime.now().strftime("%Y%m%d%H%M%S")
        fault_id = fault_id or f"FAULT-{ts}"
        session_id = session_id or fault_id
        start_time = time.time()

        logger.info(f"[FaultAgent] 开始处理故障 fault_id={fault_id}")

        # 1. 故障规划
        plan = self.planner.create_plan(fault_id, fault_description)

        # 2. 检索长期记忆
        knowledge_context = self._retrieve_knowledge(plan.knowledge_query)

        # 3. 动态构建系统 Prompt（含服务拓扑 + 知识库上下文）
        system_prompt = self._build_system_prompt(
            service_info=plan.raw_service_info,
            knowledge_context=knowledge_context,
        )

        # 4. 构建输入消息
        fault_message = build_fault_input_message(
            fault_description=fault_description,
            fault_id=fault_id,
        )

        # 5. 获取历史对话（短期记忆）
        history_messages = self.short_term_memory.get_messages(session_id)

        # 6. 创建 Agent（每次请求动态注入 system_prompt 和 middleware）
        #    create_agent 直接接收 middleware 列表，无需 callbacks 传递
        agent = create_fault_agent(
            system_prompt=system_prompt,
            fault_id=fault_id,
            console_confirm_mode=self.console_confirm_mode,
            enable_audit_log=self.enable_audit_log,
        )

        # 7. 构建消息序列（历史对话 + 当前故障消息）
        #    system_prompt 已在 create_agent 中注入，无需手动添加 SystemMessage
        messages = history_messages + [fault_message]

        # 8. 执行 Agent
        #    thread_id 用于 checkpointer 的状态隔离
        config = RunnableConfig(
            configurable={"thread_id": session_id},
            tags=[f"fault_id:{fault_id}", f"service:{plan.service_name}"],
        )

        try:
            result = agent.invoke({"messages": messages}, config=config)
            output_messages = result.get("messages", [])
            final_response = self._extract_final_response(output_messages)

            # 9. 将对话存入短期记忆
            self.short_term_memory.add_user_message(session_id, fault_message.content)
            self.short_term_memory.add_ai_message(session_id, final_response)

            elapsed = time.time() - start_time
            logger.info(
                f"[FaultAgent] 故障处理完成 fault_id={fault_id} elapsed={elapsed:.1f}s"
            )

            return {
                "fault_id": fault_id,
                "session_id": session_id,
                "service_name": plan.service_name,
                "alert_type": plan.alert_type.value,
                "response": final_response,
                "elapsed_seconds": round(elapsed, 1),
                "status": "completed",
                "messages": [{"role": "assistant", "content": final_response}],
            }

        except PermissionError as e:
            # HumanConfirmMiddleware 拦截危险操作并被拒绝
            elapsed = time.time() - start_time
            logger.warning(f"[FaultAgent] 操作被拒绝: {e}")
            return {
                "fault_id": fault_id,
                "session_id": session_id,
                "service_name": plan.service_name,
                "alert_type": plan.alert_type.value,
                "response": f"故障处理已暂停：{str(e)}",
                "elapsed_seconds": round(elapsed, 1),
                "status": "rejected",
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
        继续一个已有故障处理对话（多轮交互）

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

    def _build_system_prompt(self, service_info: str, knowledge_context: str) -> str:
        """构建系统 Prompt"""
        from prompts import get_system_prompt
        return get_system_prompt(
            service_node_info=service_info,
            long_term_memory_context=knowledge_context,
        )

    def _extract_final_response(self, messages: List) -> str:
        """从 Agent 输出消息中提取最终的 AI 响应文本"""
        for msg in reversed(messages):
            if isinstance(msg, AIMessage) and msg.content:
                if isinstance(msg.content, str):
                    return msg.content
                elif isinstance(msg.content, list):
                    text_parts = [
                        part["text"] for part in msg.content
                        if isinstance(part, dict) and part.get("type") == "text"
                    ]
                    if text_parts:
                        return "\n".join(text_parts)
        return "（Agent 未返回有效响应）"

    def verify_recovery(
        self,
        session_id: str,
        fault_id: str,
        executed_actions: str,
    ) -> Dict[str, Any]:
        """
        执行故障恢复验证

        在故障处理操作执行后调用，多次验证服务状态。
        """
        verification_prompt = (
            f"故障处理操作已完成：{executed_actions}\n\n"
            f"请重新监控以下关键指标，判断故障是否已恢复：\n"
            f"1. 服务进程是否正常运行\n"
            f"2. 接口响应是否恢复正常\n"
            f"3. 错误日志是否停止增长\n\n"
            f"如果故障已恢复，请发送恢复通知。\n"
            f"如果未完全恢复，请继续分析剩余问题。"
        )
        return self.handle_fault(
            fault_description=verification_prompt,
            session_id=session_id,
            fault_id=f"{fault_id}-VERIFY",
        )
