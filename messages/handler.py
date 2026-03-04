"""
Messages 模块 - 封装 LangChain Messages 的构建和处理逻辑
"""
from datetime import datetime
from typing import Any, Dict, List, Optional

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

from config.settings import settings


class MessageHandler:
    """消息处理器：负责构建、转换和格式化 LangChain Messages"""

    @staticmethod
    def build_human_message(content: str, metadata: Optional[Dict[str, Any]] = None) -> HumanMessage:
        """构建用户消息"""
        msg = HumanMessage(content=content)
        if metadata:
            msg.additional_kwargs.update(metadata)
        return msg

    @staticmethod
    def build_system_message(content: str) -> SystemMessage:
        """构建系统消息"""
        return SystemMessage(content=content)

    @staticmethod
    def build_ai_message(content: str) -> AIMessage:
        """构建 AI 消息"""
        return AIMessage(content=content)

    @staticmethod
    def build_tool_message(content: str, tool_call_id: str) -> ToolMessage:
        """构建工具调用结果消息"""
        return ToolMessage(content=content, tool_call_id=tool_call_id)

    @staticmethod
    def format_messages_to_text(messages: List[BaseMessage]) -> str:
        """将消息列表格式化为可读文本"""
        lines = []
        for msg in messages:
            role = type(msg).__name__.replace("Message", "")
            lines.append(f"[{role}]: {msg.content}")
        return "\n".join(lines)

    @staticmethod
    def extract_last_ai_content(messages: List[BaseMessage]) -> Optional[str]:
        """从消息列表中提取最后一条 AI 消息内容"""
        for msg in reversed(messages):
            if isinstance(msg, AIMessage):
                return msg.content
        return None

    @staticmethod
    def messages_to_dict_list(messages: List[BaseMessage]) -> List[Dict[str, Any]]:
        """将消息列表转换为字典列表（用于序列化）"""
        result = []
        for msg in messages:
            result.append({
                "type": type(msg).__name__,
                "content": msg.content,
                "additional_kwargs": msg.additional_kwargs,
            })
        return result


def build_fault_input_message(
    fault_description: str,
    fault_id: Optional[str] = None,
    timestamp: Optional[str] = None,
) -> HumanMessage:
    """
    构建故障输入消息

    Args:
        fault_description: 故障描述文本
        fault_id: 故障唯一 ID
        timestamp: 故障时间戳

    Returns:
        HumanMessage: 构建好的用户消息
    """
    ts = timestamp or datetime.now().isoformat()
    fid = fault_id or f"FAULT-{datetime.now().strftime('%Y%m%d%H%M%S')}"

    content = f"""故障ID: {fid}
故障时间: {ts}
故障描述: {fault_description}

请开始分析并处理此故障。"""

    return HumanMessage(
        content=content,
        additional_kwargs={"fault_id": fid, "timestamp": ts},
    )


def build_system_message(
    service_node_info: str = "",
    long_term_memory_context: str = "",
) -> SystemMessage:
    """
    构建系统消息（从 prompts 模块调用，此处为工厂方法）

    Args:
        service_node_info: 服务节点信息
        long_term_memory_context: 长期记忆上下文

    Returns:
        SystemMessage
    """
    from prompts import get_system_prompt
    content = get_system_prompt(
        service_node_info=service_node_info,
        long_term_memory_context=long_term_memory_context,
    )
    return SystemMessage(content=content)
