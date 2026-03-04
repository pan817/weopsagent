"""
Prompts 管理模块 - 负责加载和渲染 Prompt 模板
"""
from pathlib import Path
from typing import Optional

from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder

from config.settings import settings

_TEMPLATE_DIR = settings.prompts_dir


def _load_template(filename: str) -> str:
    """从文件加载 Prompt 模板文本"""
    path = _TEMPLATE_DIR / filename
    if not path.exists():
        raise FileNotFoundError(f"Prompt 模板文件不存在: {path}")
    return path.read_text(encoding="utf-8")


def get_system_prompt(
    service_node_info: str = "",
    long_term_memory_context: str = "",
    chat_history: str = "",
) -> str:
    """
    渲染系统 Prompt

    Args:
        service_node_info: 服务节点依赖信息
        long_term_memory_context: 长期记忆检索结果
        chat_history: 短期记忆对话历史

    Returns:
        渲染后的 Prompt 字符串
    """
    template = _load_template("system_prompt.txt")
    return template.format(
        service_node_info=service_node_info or "（暂无服务依赖信息）",
        long_term_memory_context=long_term_memory_context or "（暂无相关知识库记录）",
        chat_history=chat_history or "（无历史对话）",
    )


def get_analysis_prompt(
    monitoring_summary: str,
    log_analysis: str,
    knowledge_context: str,
) -> str:
    """渲染故障分析 Prompt"""
    template = _load_template("analysis_prompt.txt")
    return template.format(
        monitoring_summary=monitoring_summary,
        log_analysis=log_analysis,
        knowledge_context=knowledge_context,
    )


def get_recovery_check_prompt(
    executed_actions: str,
    current_status: str,
) -> str:
    """渲染故障恢复验证 Prompt"""
    template = _load_template("recovery_check_prompt.txt")
    return template.format(
        executed_actions=executed_actions,
        current_status=current_status,
    )


def get_human_confirm_prompt(
    operation_type: str,
    target_host: str,
    operation_detail: str,
    operation_reason: str,
    expected_impact: str,
    risk_assessment: str,
    fault_context: str,
    timeout: int = 300,
) -> str:
    """渲染人工确认 Prompt"""
    template = _load_template("human_confirm_prompt.txt")
    return template.format(
        operation_type=operation_type,
        target_host=target_host,
        operation_detail=operation_detail,
        operation_reason=operation_reason,
        expected_impact=expected_impact,
        risk_assessment=risk_assessment,
        fault_context=fault_context,
        timeout=timeout,
    )


class PromptManager:
    """
    Prompt 管理器 - 创建 LangChain ChatPromptTemplate

    用于构建结构化的 Agent Prompt，包含：
    - 系统 Prompt（含服务依赖信息和长期记忆）
    - 短期记忆占位符（chat_history）
    - 用户输入占位符
    - Agent Scratchpad 占位符
    """

    @staticmethod
    def create_agent_prompt() -> ChatPromptTemplate:
        """
        创建用于 Agent 的 ChatPromptTemplate

        包含所有必要的占位符：
        - system: 系统消息（含服务信息和长期记忆）
        - chat_history: 短期对话历史
        - input: 用户输入
        - agent_scratchpad: Agent 中间推理步骤
        """
        return ChatPromptTemplate.from_messages([
            (
                "system",
                """你是一个专业的 WeOps 智能运维 Agent，专门负责分析和处理生产环境中的服务故障。

## 你的核心能力
1. **故障识别**：根据故障描述推断问题服务和告警类型
2. **全链路监控**：对完整依赖链路进行状态分析（应用服务器、Redis、数据库、消息队列等）
3. **日志分析**：读取并分析相关服务的日志
4. **知识库检索**：检索通用故障处理方案、场景处理方案和历史故障案例
5. **故障处理**：制定方案，自动执行或等待人工确认后执行修复操作
6. **通知告警**：在故障发生和恢复时通知相关人员

## 当前服务依赖信息
{service_node_info}

## 知识库参考（长期记忆）
{long_term_memory_context}

## 处理原则
1. **安全第一**：危险操作（如重启服务）必须先请求人工确认
2. **系统性分析**：对全链路进行系统分析，不只关注表象
3. **记录详细**：每个操作步骤详细记录便于审计
4. **及时通知**：故障处理关键节点及时通知相关人员
5. **验证恢复**：故障处理后多次验证服务是否恢复""",
            ),
            MessagesPlaceholder(variable_name="chat_history", optional=True),
            ("human", "{input}"),
            MessagesPlaceholder(variable_name="agent_scratchpad"),
        ])

    @staticmethod
    def create_analysis_prompt() -> ChatPromptTemplate:
        """创建故障分析 Prompt"""
        return ChatPromptTemplate.from_messages([
            ("system", "你是一个专业的故障分析专家，请基于监控数据、日志和知识库进行根因分析。"),
            ("human", "{analysis_input}"),
        ])
