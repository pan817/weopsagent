"""
短期记忆模块 - 使用 LangChain 内置记忆实现类管理对话历史

使用 InMemoryChatMessageHistory（langchain-core 1.x 内置短期记忆，
原 langchain_community.chat_message_histories.ChatMessageHistory 已迁移至此）
存储每个对话 session 的对话历史，支持按 session_id 隔离。
"""
import logging
from typing import Dict, List, Optional

from langchain_core.chat_history import BaseChatMessageHistory, InMemoryChatMessageHistory
from langchain_core.messages import BaseMessage, HumanMessage, AIMessage

from config.settings import settings

logger = logging.getLogger(__name__)


class ShortTermMemory:
    """
    短期记忆管理器

    基于 LangChain 的 InMemoryChatMessageHistory 实现，
    为每个 session_id 维护独立的对话历史。

    特性：
    - 支持多 session 隔离
    - 基于滑动窗口限制历史长度
    - 支持导入/导出对话历史
    """

    def __init__(self, max_messages: int = None):
        """
        初始化短期记忆

        Args:
            max_messages: 每个 session 保存的最大消息数（默认使用配置值）
        """
        self.max_messages = max_messages or settings.memory_window_size * 2
        # session_id -> InMemoryChatMessageHistory
        self._stores: Dict[str, InMemoryChatMessageHistory] = {}

    def get_session_history(self, session_id: str) -> InMemoryChatMessageHistory:
        """
        获取指定 session 的对话历史（LangChain 标准接口）

        Args:
            session_id: 会话唯一标识

        Returns:
            InMemoryChatMessageHistory: 该 session 的消息历史对象
        """
        if session_id not in self._stores:
            self._stores[session_id] = InMemoryChatMessageHistory()
        return self._stores[session_id]

    def add_user_message(self, session_id: str, content: str) -> None:
        """添加用户消息"""
        history = self.get_session_history(session_id)
        history.add_user_message(content)
        self._trim_if_needed(session_id)

    def add_ai_message(self, session_id: str, content: str) -> None:
        """添加 AI 回复消息"""
        history = self.get_session_history(session_id)
        history.add_ai_message(content)
        self._trim_if_needed(session_id)

    def add_messages(self, session_id: str, messages: List[BaseMessage]) -> None:
        """批量添加消息"""
        history = self.get_session_history(session_id)
        history.add_messages(messages)
        self._trim_if_needed(session_id)

    def get_messages(self, session_id: str) -> List[BaseMessage]:
        """获取指定 session 的所有消息"""
        return self.get_session_history(session_id).messages

    def get_messages_as_text(self, session_id: str) -> str:
        """将消息历史格式化为文本，用于 Prompt 中的 chat_history 占位符"""
        messages = self.get_messages(session_id)
        if not messages:
            return "（无历史对话）"

        lines = []
        for msg in messages:
            if isinstance(msg, HumanMessage):
                role = "用户"
            elif isinstance(msg, AIMessage):
                role = "Agent"
            else:
                role = type(msg).__name__
            lines.append(f"{role}: {msg.content[:500]}")

        return "\n".join(lines)

    def clear_session(self, session_id: str) -> None:
        """清除指定 session 的对话历史"""
        if session_id in self._stores:
            self._stores[session_id].clear()
            logger.info(f"[ShortTermMemory] 已清除 session {session_id} 的对话历史")

    def delete_session(self, session_id: str) -> None:
        """删除指定 session"""
        self._stores.pop(session_id, None)

    def list_sessions(self) -> List[str]:
        """列出所有活跃 session"""
        return list(self._stores.keys())

    def _trim_if_needed(self, session_id: str) -> None:
        """当消息超出限制时，从最旧的消息开始裁剪"""
        history = self._stores.get(session_id)
        if history and len(history.messages) > self.max_messages:
            # 保留最新的 max_messages 条消息
            history.messages = history.messages[-self.max_messages:]
            logger.debug(
                f"[ShortTermMemory] session {session_id} 已裁剪至 {self.max_messages} 条消息"
            )

    def export_session(self, session_id: str) -> List[dict]:
        """导出 session 历史为可序列化的字典列表"""
        messages = self.get_messages(session_id)
        return [
            {"type": type(msg).__name__, "content": msg.content}
            for msg in messages
        ]

    def import_session(self, session_id: str, messages_data: List[dict]) -> None:
        """从字典列表导入历史消息"""
        history = self.get_session_history(session_id)
        history.clear()
        for item in messages_data:
            msg_type = item.get("type", "")
            content = item.get("content", "")
            if "Human" in msg_type:
                history.add_user_message(content)
            elif "AI" in msg_type:
                history.add_ai_message(content)


# 全局短期记忆单例（跨请求共享，按 session_id 隔离）
_global_short_term_memory: Optional[ShortTermMemory] = None


def get_short_term_memory() -> ShortTermMemory:
    """获取全局短期记忆单例"""
    global _global_short_term_memory
    if _global_short_term_memory is None:
        _global_short_term_memory = ShortTermMemory()
    return _global_short_term_memory
