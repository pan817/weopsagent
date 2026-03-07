"""
分析 Subagent - 故障根因分析与恢复方案制定

Subagent 设计要点：
- 编译后的 Agent 实例在进程生命周期内缓存复用，不在每次调用时重建
- Prompt 从 agents/prompts/analysis_agent.txt 加载，便于维护
- 不绑定任何工具（纯 LLM 推理），监控数据和知识库内容已通过 FaultState 传入
- 通过 Prompt 引导 LLM 输出结构化 Markdown，便于下游 RecoveryAgent 解析

无工具：所有分析基于 FaultState 中的 monitoring_results + knowledge_context 进行
"""
import logging
import re
from pathlib import Path
from typing import Any, Dict

from langchain.agents import create_agent
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver

from llm.model import get_llm
from middleware.audit_log import AuditLogMiddleware

logger = logging.getLogger(__name__)

# ===== 单例缓存 =====
_agent: Any = None


def _load_prompt() -> str:
    """从 agents/prompts/analysis_agent.txt 加载 System Prompt"""
    prompt_path = Path(__file__).parent / "prompts" / "analysis_agent.txt"
    return prompt_path.read_text(encoding="utf-8").strip()


def _get_agent() -> Any:
    """
    获取 Analysis Subagent 编译实例（进程级单例）

    分析 Agent 无工具，纯 LLM 推理，编译开销最小。
    fault_id 通过 RunnableConfig.configurable["fault_id"] 在调用时动态注入。
    """
    global _agent
    if _agent is None:
        logger.info("[AnalysisAgent] 编译 Analysis Subagent（首次初始化）")
        _agent = create_agent(
            model=get_llm(),
            tools=[],   # 分析 Agent 不需要任何工具，纯 LLM 推理
            system_prompt=_load_prompt(),
            middleware=[AuditLogMiddleware()],
            checkpointer=InMemorySaver(),
            name="analysis_agent",
        )
    return _agent


def run_analysis_node(state: Dict[str, Any], config: RunnableConfig) -> Dict[str, Any]:
    """
    分析节点（LangGraph 节点函数）

    读取 monitoring_results、knowledge_context、service_node_info，
    调用 Analysis Subagent 输出 analysis_result、root_cause、recovery_plan。
    """
    fault_id = state.get("fault_id", "UNKNOWN")
    fault_description = state.get("fault_description", "")
    monitoring_results = state.get("monitoring_results", "")
    knowledge_context = state.get("knowledge_context", "")
    service_node_info = state.get("service_node_info", "")

    logger.info(f"[AnalysisAgent] 开始分析 fault_id={fault_id}")

    prompt = f"""## 故障描述
{fault_description}

## 服务依赖信息
{service_node_info}

## 全链路监控摘要
{monitoring_results}

## 知识库参考（RAG 检索结果）
{knowledge_context}

请基于以上信息进行完整的根因分析，并给出恢复方案。"""

    try:
        agent = _get_agent()
        result = agent.invoke(
            {"messages": [HumanMessage(content=prompt)]},
            config=RunnableConfig(
                configurable={
                    "thread_id": f"{fault_id}-analysis",
                    "fault_id": fault_id,   # 供 AuditLogMiddleware 动态读取
                },
            ),
        )
        messages = result.get("messages", [])
        analysis_text = _extract_last_text(messages)

        root_cause = _extract_section(analysis_text, "根因分析")
        recovery_plan = _extract_section(analysis_text, "恢复方案")

        logger.info(f"[AnalysisAgent] 分析完成 fault_id={fault_id}")

        return {
            "analysis_result": analysis_text,
            "root_cause": root_cause or analysis_text[:500],
            "recovery_plan": recovery_plan or "请参考完整分析结果",
            "messages": messages[-1:] if messages else [],
        }
    except Exception as e:
        logger.error(f"[AnalysisAgent] 分析失败 fault_id={fault_id}: {e}")
        return {
            "analysis_result": f"分析失败: {e}",
            "root_cause": "分析异常",
            "recovery_plan": "请人工介入处理",
            "error_message": f"AnalysisAgent 异常: {e}",
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
    return "（分析 Agent 未返回有效结果）"


def _extract_section(text: str, section_name: str) -> str:
    """从 Markdown 格式文本中提取指定 ### 小节的内容"""
    pattern = rf"###\s*{section_name}\s*\n(.*?)(?=###|\Z)"
    match = re.search(pattern, text, re.DOTALL)
    if match:
        return match.group(1).strip()
    return ""
