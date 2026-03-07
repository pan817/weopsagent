"""
分析 Agent - 专注故障根因分析

职责：
- 综合监控数据 + 知识库内容进行根因分析
- 制定恢复方案
- 不调用任何工具（纯 LLM 推理）

上下文来源（均从 state 传入，无需工具调用）：
  monitoring_results（监控摘要）、knowledge_context（RAG 检索结果）、
  service_node_info（服务拓扑）、fault_description（故障描述）
"""
import logging
from typing import Any, Dict

from langchain.agents import create_agent
from langchain_core.messages import HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver

from llm.model import get_llm
from middleware.audit_log import AuditLogMiddleware

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """你是专业的 WeOps 分析 Agent，负责对故障进行根因分析并制定恢复方案。

## 你的输入
你会收到以下信息（均已整理好，无需调用工具）：
1. 故障描述：用户上报的原始告警内容
2. 全链路监控摘要：各中间件的运行状态数据
3. 知识库参考：通用处理方案、场景处理方案、历史故障案例

## 你的职责
1. **根因分析**：基于监控数据，定位故障的直接原因和根本原因
2. **影响评估**：评估故障影响范围和严重程度
3. **方案制定**：给出具体可执行的恢复方案（按优先级排序）
4. **方案说明**：说明每个操作步骤的目的和预期效果

## 输出格式
请按以下结构输出：

### 根因分析
[直接原因和根本原因的详细描述]

### 影响评估
[故障影响范围、严重程度、受影响用户估算]

### 恢复方案
1. [步骤1]（优先级：高/中/低，预期效果：xxx）
2. [步骤2]...

### 注意事项
[执行方案时需要注意的风险和操作前提]"""


def create_analysis_agent(fault_id: str = None):
    """创建分析 Agent（不绑定任何工具，纯 LLM 推理）"""
    return create_agent(
        model=get_llm(),
        tools=[],   # 分析 Agent 不需要任何工具
        system_prompt=_SYSTEM_PROMPT,
        middleware=[AuditLogMiddleware(fault_id=fault_id)],
        checkpointer=InMemorySaver(),
        name="analysis_agent",
    )


def run_analysis_node(state: Dict[str, Any], config: RunnableConfig) -> Dict[str, Any]:
    """
    分析节点执行函数（LangGraph 节点）

    读取 monitoring_results 和 knowledge_context，
    调用分析 Agent 输出 analysis_result、root_cause、recovery_plan。
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
        agent = create_analysis_agent(fault_id=fault_id)
        result = agent.invoke(
            {"messages": [HumanMessage(content=prompt)]},
            config=RunnableConfig(
                configurable={"thread_id": f"{fault_id}-analysis"},
            ),
        )
        messages = result.get("messages", [])
        analysis_text = _extract_last_text(messages)

        # 从分析结果中解析出根因和方案（简单分段提取）
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
    from langchain_core.messages import AIMessage
    for msg in reversed(messages):
        if isinstance(msg, AIMessage) and msg.content:
            if isinstance(msg.content, str):
                return msg.content
            elif isinstance(msg.content, list):
                texts = [p.get("text", "") for p in msg.content if isinstance(p, dict) and p.get("type") == "text"]
                return "\n".join(texts)
    return "（分析 Agent 未返回有效结果）"


def _extract_section(text: str, section_name: str) -> str:
    """从 Markdown 格式文本中提取指定小节内容"""
    import re
    # 匹配 ### 节标题
    pattern = rf"###\s*{section_name}\s*\n(.*?)(?=###|\Z)"
    match = re.search(pattern, text, re.DOTALL)
    if match:
        return match.group(1).strip()
    return ""
